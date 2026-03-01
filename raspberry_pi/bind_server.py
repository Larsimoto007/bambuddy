"""Bind/detect server for printer discovery (port 3000).

Bambu slicers connect to port 3000 to perform the bind/detect handshake.

Protocol:
  - Framing: 0xA5A5 + uint16_le(total_msg_size) + JSON payload + 0xA7A7
  - Slicer sends: {"login":{"command":"detect"}}
  - Printer replies with identity info

Standalone module - no external project dependencies.
"""

import asyncio
import json
import logging
import struct

logger = logging.getLogger(__name__)

BIND_PORT = 3000
FRAME_HEADER = b"\xa5\xa5"
FRAME_TRAILER = b"\xa7\xa7"
HEADER_SIZE = 4
TRAILER_SIZE = 2


class BindServer:
    """Responds to slicer bind/detect requests on port 3000."""

    def __init__(self, serial: str, model: str, name: str, version: str = "01.00.00.00"):
        self.serial = serial
        self.model = model
        self.name = name
        self.version = version
        self._server: asyncio.Server | None = None
        self._running = False

    async def start(self) -> None:
        if self._running:
            return

        logger.info("Starting bind server on port %s (serial=%s, model=%s)", BIND_PORT, self.serial, self.model)

        try:
            self._running = True
            self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", BIND_PORT)

            logger.info("Bind server listening on port %s", BIND_PORT)

            async with self._server:
                await self._server.serve_forever()

        except OSError as e:
            if e.errno == 98:
                logger.error("Bind server port %s is already in use", BIND_PORT)
            elif e.errno == 13:
                logger.error("Bind server: permission denied for port %s", BIND_PORT)
            else:
                logger.error("Bind server error: %s", e)
        except asyncio.CancelledError:
            logger.debug("Bind server task cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        self._running = False
        if self._server:
            try:
                self._server.close()
                await self._server.wait_closed()
            except OSError:
                pass
            self._server = None

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        client_id = f"{peername[0]}:{peername[1]}" if peername else "unknown"
        logger.info("Bind server: client connected from %s", client_id)

        try:
            data = await asyncio.wait_for(reader.read(4096), timeout=10.0)
            if not data:
                return

            request = self._parse_frame(data)
            if request is None:
                return

            logger.info("Bind server: received from %s: %s", client_id, request)

            login = request.get("login", {})
            if not isinstance(login, dict) or login.get("command") != "detect":
                return

            response = {
                "login": {
                    "bind": "free",
                    "command": "detect",
                    "connect": "lan",
                    "dev_cap": 1,
                    "id": self.serial,
                    "model": self.model,
                    "name": self.name,
                    "sequence_id": 3021,
                    "version": self.version,
                }
            }

            frame = self._build_frame(response)
            writer.write(frame)
            await writer.drain()

            logger.info("Bind server: sent detect response to %s", client_id)

        except TimeoutError:
            logger.debug("Bind server: timeout from %s", client_id)
        except Exception as e:
            logger.error("Bind server: error handling %s: %s", client_id, e)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    def _parse_frame(self, data: bytes) -> dict | None:
        if len(data) < HEADER_SIZE + TRAILER_SIZE:
            return None
        if data[:2] != FRAME_HEADER:
            return None
        if data[-2:] != FRAME_TRAILER:
            return None

        json_bytes = data[HEADER_SIZE:-TRAILER_SIZE]
        try:
            return json.loads(json_bytes)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def _build_frame(self, payload: dict) -> bytes:
        json_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        total_len = HEADER_SIZE + len(json_bytes) + TRAILER_SIZE
        header = FRAME_HEADER + struct.pack("<H", total_len)
        return header + json_bytes + FRAME_TRAILER
