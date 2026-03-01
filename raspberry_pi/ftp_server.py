"""Implicit FTPS server for receiving 3MF uploads from slicers.

Implements an implicit FTPS server (TLS from byte 0) that accepts file
uploads from Bambu Studio and OrcaSlicer.

Standalone module - no external project dependencies.
"""

import asyncio
import logging
import random
import ssl
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

FTP_PORT = 9990


class FTPSession:
    """Handles a single FTP client session."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upload_dir: Path,
        access_code: str,
        ssl_context: ssl.SSLContext,
        on_file_received: Callable | None,
        passive_port_range: tuple[int, int] = (50000, 50100),
    ):
        self.reader = reader
        self.writer = writer
        self.upload_dir = upload_dir
        self.access_code = access_code
        self.ssl_context = ssl_context
        self.on_file_received = on_file_received
        self.passive_port_range = passive_port_range

        self.authenticated = False
        self.username: str | None = None
        self.current_dir = upload_dir
        self.transfer_type = "A"
        self.data_server: asyncio.Server | None = None
        self.data_port: int | None = None

        self._data_reader: asyncio.StreamReader | None = None
        self._data_writer: asyncio.StreamWriter | None = None
        self._data_connected = asyncio.Event()
        self._transfer_done = asyncio.Event()

        peername = writer.get_extra_info("peername")
        self.remote_ip = peername[0] if peername else "unknown"

    async def send(self, code: int, message: str) -> None:
        response = f"{code} {message}\r\n"
        self.writer.write(response.encode("utf-8"))
        await self.writer.drain()

    async def handle(self) -> None:
        try:
            await self.send(220, "Bambuddy Middleware FTP ready")

            while True:
                try:
                    line = await asyncio.wait_for(self.reader.readline(), timeout=300)
                except TimeoutError:
                    break

                if not line:
                    break

                try:
                    command_line = line.decode("utf-8").strip()
                except UnicodeDecodeError:
                    command_line = line.decode("latin-1").strip()

                if not command_line:
                    continue

                logger.info("FTP <- %s: %s", self.remote_ip, command_line)

                parts = command_line.split(" ", 1)
                cmd = parts[0].upper()
                arg = parts[1] if len(parts) > 1 else ""

                handler = getattr(self, f"cmd_{cmd}", None)
                if handler:
                    await handler(arg)
                else:
                    await self.send(502, f"Command {cmd} not implemented")

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error("FTP session error from %s: %s", self.remote_ip, e)
        finally:
            await self._cleanup()

    async def _cleanup(self) -> None:
        self._transfer_done.set()

        if self.data_server:
            self.data_server.close()
            try:
                await self.data_server.wait_closed()
            except OSError:
                pass
            self.data_server = None

        try:
            self.writer.close()
            await self.writer.wait_closed()
        except OSError:
            pass

    async def cmd_USER(self, arg: str) -> None:
        self.username = arg
        if arg.lower() == "bblp":
            await self.send(331, "Password required")
        else:
            await self.send(530, "Invalid user")

    async def cmd_PASS(self, arg: str) -> None:
        if self.username and self.username.lower() == "bblp":
            if arg == self.access_code:
                self.authenticated = True
                await self.send(230, "Login successful")
            else:
                await self.send(530, "Login incorrect")
        else:
            await self.send(503, "Login with USER first")

    async def cmd_SYST(self, arg: str) -> None:
        await self.send(215, "UNIX Type: L8")

    async def cmd_FEAT(self, arg: str) -> None:
        features = ["211-Features:", " PASV", " EPSV", " UTF8", " SIZE", "211 End"]
        for line in features[:-1]:
            self.writer.write(f"{line}\r\n".encode())
        await self.writer.drain()
        self.writer.write(f"{features[-1]}\r\n".encode())
        await self.writer.drain()

    async def cmd_PWD(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        await self.send(257, '"/" is current directory')

    async def cmd_CWD(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        await self.send(250, "Directory changed")

    async def cmd_TYPE(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        if arg.upper() in ("A", "I"):
            self.transfer_type = arg.upper()
            type_name = "ASCII" if arg.upper() == "A" else "Binary"
            await self.send(200, f"Type set to {type_name}")
        else:
            await self.send(504, "Type not supported")

    async def _bind_passive_port(self) -> bool:
        port_min, port_max = self.passive_port_range
        for _ in range(10):
            port = random.randint(port_min, port_max)
            try:
                self.data_server = await asyncio.start_server(
                    self._handle_data_connection,
                    "0.0.0.0",
                    port,
                    ssl=self.ssl_context,
                )
                self.data_port = port
                return True
            except OSError:
                pass
        return False

    async def cmd_EPSV(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return

        await self._close_data_connection()
        self._data_connected.clear()
        self._data_reader = None
        self._data_writer = None
        self._transfer_done = asyncio.Event()

        if await self._bind_passive_port():
            await self.send(229, f"Entering Extended Passive Mode (|||{self.data_port}|)")
        else:
            await self.send(425, "Cannot open data connection")

    async def cmd_PASV(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return

        await self._close_data_connection()
        self._data_connected.clear()
        self._data_reader = None
        self._data_writer = None
        self._transfer_done = asyncio.Event()

        if await self._bind_passive_port():
            sockname = self.writer.get_extra_info("sockname")
            ip = sockname[0] if sockname else "127.0.0.1"
            if ip == "0.0.0.0":
                ip = "127.0.0.1"

            ip_parts = ip.split(".")
            port_hi = self.data_port // 256
            port_lo = self.data_port % 256

            await self.send(
                227,
                f"Entering Passive Mode ({ip_parts[0]},{ip_parts[1]},{ip_parts[2]},{ip_parts[3]},{port_hi},{port_lo})",
            )
        else:
            await self.send(425, "Cannot open data connection")

    async def _handle_data_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._data_reader is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
            return

        self._data_reader = reader
        self._data_writer = writer

        if self.data_server:
            self.data_server.close()

        self._data_connected.set()
        await self._transfer_done.wait()

    async def _close_data_connection(self) -> None:
        had_connection = self._data_writer is not None or self.data_server is not None
        self._transfer_done.set()

        if self._data_writer:
            try:
                self._data_writer.close()
                await self._data_writer.wait_closed()
            except OSError:
                pass
            self._data_writer = None
            self._data_reader = None

        if self.data_server:
            try:
                self.data_server.close()
                await self.data_server.wait_closed()
            except OSError:
                pass
            self.data_server = None

        if had_connection:
            await asyncio.sleep(0.1)

    async def cmd_STOR(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return

        if not self.data_server and not self._data_connected.is_set():
            await self.send(425, "Use PASV first")
            return

        filename = Path(arg).name
        file_path = self.upload_dir / filename

        logger.info("FTP receiving file: %s from %s", filename, self.remote_ip)

        await self.send(150, f"Opening data connection for {filename}")

        try:
            await asyncio.wait_for(self._data_connected.wait(), timeout=30)
        except TimeoutError:
            await self.send(425, "Data connection timeout")
            await self._close_data_connection()
            return

        if not self._data_reader:
            await self.send(425, "Data connection failed")
            await self._close_data_connection()
            return

        data_content: list[bytes] = []
        total_received = 0
        try:
            while True:
                chunk = await asyncio.wait_for(self._data_reader.read(65536), timeout=60)
                if not chunk:
                    break
                data_content.append(chunk)
                total_received += len(chunk)
        except TimeoutError:
            await self.send(426, "Transfer timeout")
            await self._close_data_connection()
            return
        except Exception as e:
            await self.send(426, f"Transfer failed: {e}")
            await self._close_data_connection()
            return

        await self._close_data_connection()

        try:
            file_path.write_bytes(b"".join(data_content))
            logger.info("FTP saved file: %s (%s bytes)", file_path, total_received)
            await self.send(226, "Transfer complete")

            if self.on_file_received:
                try:
                    result = self.on_file_received(file_path, self.remote_ip)
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as e:
                    logger.error("File received callback error: %s", e)

        except Exception as e:
            logger.error("Failed to save file %s: %s", file_path, e)
            await self.send(550, "Failed to save file")

    async def cmd_SIZE(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        await self.send(550, "File not found")

    async def cmd_QUIT(self, arg: str) -> None:
        await self.send(221, "Goodbye")
        raise asyncio.CancelledError()

    async def cmd_NOOP(self, arg: str) -> None:
        await self.send(200, "OK")

    async def cmd_OPTS(self, arg: str) -> None:
        if arg.upper().startswith("UTF8"):
            await self.send(200, "UTF8 mode enabled")
        else:
            await self.send(501, "Option not supported")

    async def cmd_PBSZ(self, arg: str) -> None:
        await self.send(200, "PBSZ=0")

    async def cmd_PROT(self, arg: str) -> None:
        if arg.upper() == "P":
            await self.send(200, "Protection level set to Private")
        elif arg.upper() == "C":
            await self.send(536, "Protection level C not supported")
        else:
            await self.send(504, f"Protection level {arg} not supported")

    async def cmd_MKD(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        await self.send(257, f'"{arg}" directory created')

    async def cmd_LIST(self, arg: str) -> None:
        if not self.authenticated:
            await self.send(530, "Not logged in")
            return
        await self.send(150, "Opening data connection")
        await self.send(226, "Transfer complete")


class FTPServer:
    """Implicit FTPS server that accepts uploads from slicers."""

    PASSIVE_PORT_MIN = 50000
    PASSIVE_PORT_MAX = 50100

    def __init__(
        self,
        upload_dir: Path,
        access_code: str,
        cert_path: Path,
        key_path: Path,
        port: int = FTP_PORT,
        on_file_received: Callable | None = None,
    ):
        self.upload_dir = upload_dir
        self.access_code = access_code
        self.cert_path = cert_path
        self.key_path = key_path
        self.port = port
        self.on_file_received = on_file_received
        self._server: asyncio.Server | None = None
        self._running = False
        self._ssl_context: ssl.SSLContext | None = None
        self._active_sessions: list[asyncio.Task] = []

    async def start(self) -> None:
        if self._running:
            return

        logger.info("Starting implicit FTPS on port %s", self.port)

        self.upload_dir.mkdir(parents=True, exist_ok=True)
        (self.upload_dir / "cache").mkdir(exist_ok=True)

        self._ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._ssl_context.load_cert_chain(str(self.cert_path), str(self.key_path))
        self._ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_context.maximum_version = ssl.TLSVersion.TLSv1_2
        self._ssl_context.set_ciphers("HIGH:!aNULL:!MD5:!RC4")

        try:
            self._server = await asyncio.start_server(
                self._handle_client,
                "0.0.0.0",
                self.port,
                ssl=self._ssl_context,
            )
            self._running = True

            logger.info("Implicit FTPS server started on port %s", self.port)

            async with self._server:
                await self._server.serve_forever()

        except OSError as e:
            if e.errno == 98:
                logger.error("FTP port %s is already in use", self.port)
            else:
                logger.error("FTP server error: %s", e)
        except asyncio.CancelledError:
            pass
        finally:
            await self.stop()

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        logger.info("FTP connection from %s", peername)

        session = FTPSession(
            reader=reader,
            writer=writer,
            upload_dir=self.upload_dir,
            access_code=self.access_code,
            ssl_context=self._ssl_context,
            on_file_received=self.on_file_received,
            passive_port_range=(self.PASSIVE_PORT_MIN, self.PASSIVE_PORT_MAX),
        )

        task = asyncio.current_task()
        if task:
            self._active_sessions.append(task)
        try:
            await session.handle()
        finally:
            if task and task in self._active_sessions:
                self._active_sessions.remove(task)

    async def stop(self) -> None:
        self._running = False

        for task in self._active_sessions[:]:
            task.cancel()

        if self._active_sessions:
            await asyncio.sleep(0.1)

        self._active_sessions.clear()

        if self._server:
            try:
                self._server.close()
                await self._server.wait_closed()
            except OSError:
                pass
            self._server = None
