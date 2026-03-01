"""SSDP discovery responder for the middleware.

Responds to M-SEARCH requests from slicers and sends periodic NOTIFY
announcements so the middleware appears as a discoverable Bambu printer.

Standalone module - no external project dependencies.
"""

import asyncio
import logging
import socket
import struct

logger = logging.getLogger(__name__)

SSDP_MULTICAST_ADDR = "239.255.255.250"
SSDP_BROADCAST_ADDR = "255.255.255.255"
SSDP_PORT = 2021

BAMBU_SEARCH_TARGET = "urn:bambulab-com:device:3dprinter:1"


class SSDPServer:
    """SSDP server that responds to discovery requests as a Bambu printer."""

    def __init__(
        self,
        name: str = "Bambuddy Middleware",
        serial: str = "00M09A391800001",
        model: str = "3DPrinter-X1-Carbon",
    ):
        self.name = name
        self.serial = serial
        self.model = model
        self._running = False
        self._socket: socket.socket | None = None
        self._local_ip: str | None = None

    def _get_local_ip(self) -> str:
        if self._local_ip:
            return self._local_ip

        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            self._local_ip = ip
            return ip
        except OSError:
            return "127.0.0.1"

    def _build_notify_message(self) -> bytes:
        ip = self._get_local_ip()
        message = (
            "NOTIFY * HTTP/1.1\r\n"
            f"Host: {SSDP_MULTICAST_ADDR}:1990\r\n"
            "Server: UPnP/1.0\r\n"
            f"Location: {ip}\r\n"
            f"NT: {BAMBU_SEARCH_TARGET}\r\n"
            "NTS: ssdp:alive\r\n"
            f"USN: {self.serial}\r\n"
            "Cache-Control: max-age=1800\r\n"
            f"DevModel.bambu.com: {self.model}\r\n"
            f"DevName.bambu.com: {self.name}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "Devseclink.bambu.com: secure\r\n"
            "DevInf.bambu.com: eth0\r\n"
            "DevVersion.bambu.com: 01.07.00.00\r\n"
            "DevCap.bambu.com: 1\r\n"
            "\r\n"
        )
        return message.encode()

    def _build_response_message(self) -> bytes:
        ip = self._get_local_ip()
        message = (
            "HTTP/1.1 200 OK\r\n"
            "Server: UPnP/1.0\r\n"
            f"Location: {ip}\r\n"
            f"ST: {BAMBU_SEARCH_TARGET}\r\n"
            f"USN: {self.serial}\r\n"
            "Cache-Control: max-age=1800\r\n"
            f"DevModel.bambu.com: {self.model}\r\n"
            f"DevName.bambu.com: {self.name}\r\n"
            "DevSignal.bambu.com: -44\r\n"
            "DevConnect.bambu.com: lan\r\n"
            "DevBind.bambu.com: free\r\n"
            "Devseclink.bambu.com: secure\r\n"
            "DevInf.bambu.com: eth0\r\n"
            "DevVersion.bambu.com: 01.07.00.00\r\n"
            "DevCap.bambu.com: 1\r\n"
            "\r\n"
        )
        return message.encode()

    async def start(self) -> None:
        if self._running:
            return

        logger.info("Starting SSDP server: %s (%s)", self.name, self.serial)
        self._running = True

        try:
            self._socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

            try:
                self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except (AttributeError, OSError):
                pass

            self._socket.setblocking(False)
            self._socket.bind(("", SSDP_PORT))

            mreq = struct.pack("4sl", socket.inet_aton(SSDP_MULTICAST_ADDR), socket.INADDR_ANY)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            self._socket.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)

            local_ip = self._get_local_ip()
            logger.info("SSDP server listening on port %s, advertising IP: %s", SSDP_PORT, local_ip)

            await self._send_notify()

            last_notify = asyncio.get_event_loop().time()
            notify_interval = 30.0

            while self._running:
                try:
                    data, addr = self._socket.recvfrom(4096)
                    message = data.decode("utf-8", errors="ignore")
                    await self._handle_message(message, addr)
                except BlockingIOError:
                    pass
                except OSError as e:
                    if self._running:
                        logger.debug("SSDP receive error: %s", e)

                now = asyncio.get_event_loop().time()
                if now - last_notify >= notify_interval:
                    await self._send_notify()
                    last_notify = now

                await asyncio.sleep(0.1)

        except OSError as e:
            if e.errno == 98:
                logger.warning("SSDP port %s in use", SSDP_PORT)
            else:
                logger.error("SSDP server error: %s", e)
        except asyncio.CancelledError:
            logger.debug("SSDP server cancelled")
        finally:
            await self._cleanup()

    async def stop(self) -> None:
        self._running = False
        await self._cleanup()

    async def _cleanup(self) -> None:
        if self._socket:
            try:
                await self._send_byebye()
            except OSError:
                pass
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

    async def _send_notify(self) -> None:
        if not self._socket:
            return
        try:
            msg = self._build_notify_message()
            self._socket.sendto(msg, (SSDP_BROADCAST_ADDR, SSDP_PORT))
        except OSError as e:
            logger.debug("Failed to send NOTIFY: %s", e)

    async def _send_byebye(self) -> None:
        if not self._socket:
            return

        message = (
            "NOTIFY * HTTP/1.1\r\n"
            f"Host: {SSDP_MULTICAST_ADDR}:1990\r\n"
            f"NT: {BAMBU_SEARCH_TARGET}\r\n"
            "NTS: ssdp:byebye\r\n"
            f"USN: {self.serial}\r\n"
            "\r\n"
        )

        try:
            self._socket.sendto(message.encode(), (SSDP_BROADCAST_ADDR, SSDP_PORT))
        except OSError:
            pass

    async def _handle_message(self, message: str, addr: tuple[str, int]) -> None:
        if "M-SEARCH" not in message:
            return

        if BAMBU_SEARCH_TARGET not in message and "ssdp:all" not in message.lower():
            return

        logger.debug("Received M-SEARCH from %s", addr[0])

        if self._socket:
            try:
                response = self._build_response_message()
                self._socket.sendto(response, addr)
                logger.info("Sent SSDP response to %s for '%s'", addr[0], self.name)
            except OSError as e:
                logger.debug("Failed to send SSDP response: %s", e)
