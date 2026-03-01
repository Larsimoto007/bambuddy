#!/usr/bin/env python3
"""Bambuddy Middleware - Raspberry Pi 3D Printer Bridge.

Makes any USB-connected G-code 3D printer appear as a BambuLab printer
to Bambu Studio, OrcaSlicer, and the BamBuddy management software.

Run on a Raspberry Pi connected to a 3D printer via USB cable.

Usage:
    python3 bambuddy_middleware.py
    python3 bambuddy_middleware.py --serial-port /dev/ttyACM0 --baudrate 250000
    python3 bambuddy_middleware.py --config config.json

Services started:
    - SSDP (port 2021)  : Printer discovery on the network
    - MQTT (port 8883)  : Slicer command/status communication (TLS)
    - FTP  (port 9990)  : File upload from slicers (implicit FTPS)
    - Bind (port 3000)  : Slicer bind/detect handshake
    - Serial            : USB communication with physical printer

Architecture:
    Bambu Studio / OrcaSlicer
         │
         ├─ SSDP discover ──► SSDPServer (port 2021)
         ├─ Bind/detect ────► BindServer (port 3000)
         ├─ MQTT commands ──► MQTTServer (port 8883, TLS)
         │                        │
         │                   GCodeMiddleware ──► SerialConnection
         │                        │                    │
         │                   (translates)          (USB cable)
         │                        │                    │
         │                        ▼                    ▼
         └─ FTP upload ─────► FTPServer  ──► Physical 3D Printer
              (port 9990)                   (Marlin/Klipper/etc.)
"""

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
from pathlib import Path

# Local modules (same directory)
from bind_server import BindServer
from certificate import CertificateService
from ftp_server import FTPServer
from gcode_middleware import GCodeMiddleware
from mqtt_server import MQTTServer
from serial_connection import SerialConnection
from ssdp_server import SSDPServer

# ============================================================================
# Default configuration
# ============================================================================

DEFAULT_CONFIG = {
    # Access code for slicer authentication (8 characters, like real Bambu printers)
    "access_code": "12345678",

    # Printer name shown in slicer discovery
    "printer_name": "Bambuddy Middleware",

    # Serial number (used in SSDP, MQTT, certificates)
    "serial": "00M09A391800001",

    # Model code (determines how slicer treats the printer)
    # Options: "3DPrinter-X1-Carbon", "C11" (P1S), "C12" (P1P), "N1" (A1)
    "model": "3DPrinter-X1-Carbon",

    # Serial port for the physical printer
    "serial_port": "/dev/ttyUSB0",

    # Baud rate for serial communication
    "baudrate": 115200,

    # Data directory for certificates and uploads
    "data_dir": "/var/lib/bambuddy-middleware",

    # Log level
    "log_level": "INFO",
}

# ============================================================================
# Logging setup
# ============================================================================

def setup_logging(level: str = "INFO") -> None:
    """Configure logging with timestamps and module info."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )


# ============================================================================
# Main middleware service
# ============================================================================

class MiddlewareService:
    """Coordinates all middleware services."""

    def __init__(self, config: dict):
        self.config = config
        self.logger = logging.getLogger("middleware")

        self._data_dir = Path(config["data_dir"])
        self._upload_dir = self._data_dir / "uploads"
        self._cert_dir = self._data_dir / "certs"

        self._ssdp: SSDPServer | None = None
        self._mqtt: MQTTServer | None = None
        self._ftp: FTPServer | None = None
        self._bind: BindServer | None = None
        self._middleware: GCodeMiddleware | None = None
        self._tasks: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        """Start all middleware services."""
        self.logger.info("=" * 60)
        self.logger.info("Bambuddy Middleware starting...")
        self.logger.info("=" * 60)

        # Create directories
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._upload_dir.mkdir(parents=True, exist_ok=True)
        (self._upload_dir / "cache").mkdir(exist_ok=True)

        # Generate TLS certificates
        cert_service = CertificateService(
            cert_dir=self._cert_dir,
            serial=self.config["serial"],
        )
        cert_path, key_path = cert_service.ensure_certificates()
        self.logger.info("TLS certificates ready at %s", self._cert_dir)

        # Initialize G-code middleware (serial connection)
        self._middleware = GCodeMiddleware(
            serial_port=self.config["serial_port"],
            baudrate=self.config["baudrate"],
            upload_dir=self._upload_dir,
        )

        connected = await self._middleware.start()
        if not connected:
            self.logger.warning(
                "Could not connect to printer on %s. "
                "Services will start anyway - printer can be connected later.",
                self.config["serial_port"],
            )

        # Initialize SSDP (printer discovery)
        self._ssdp = SSDPServer(
            name=self.config["printer_name"],
            serial=self.config["serial"],
            model=self.config["model"],
        )

        # Initialize MQTT (slicer commands/status)
        self._mqtt = MQTTServer(
            serial=self.config["serial"],
            access_code=self.config["access_code"],
            cert_path=cert_path,
            key_path=key_path,
            on_print_command=self._on_print_command,
            on_mqtt_command=self._on_mqtt_command,
        )

        # Connect MQTT status reports to middleware
        self._mqtt.set_status_provider(self._middleware.get_bambu_status)

        # Initialize FTP (file uploads)
        self._ftp = FTPServer(
            upload_dir=self._upload_dir,
            access_code=self.config["access_code"],
            cert_path=cert_path,
            key_path=key_path,
            on_file_received=self._on_file_received,
        )

        # Initialize Bind server (slicer handshake)
        self._bind = BindServer(
            serial=self.config["serial"],
            model=self.config["model"],
            name=self.config["printer_name"],
        )

        # Start all services as background tasks
        self._running = True

        async def run_service(coro, name):
            try:
                await coro
            except Exception as e:
                self.logger.error("Service %s failed: %s", name, e)

        self._tasks = [
            asyncio.create_task(run_service(self._ssdp.start(), "SSDP"), name="ssdp"),
            asyncio.create_task(run_service(self._ftp.start(), "FTP"), name="ftp"),
            asyncio.create_task(run_service(self._mqtt.start(), "MQTT"), name="mqtt"),
            asyncio.create_task(run_service(self._bind.start(), "Bind"), name="bind"),
        ]

        self.logger.info("-" * 60)
        self.logger.info("Middleware services started:")
        self.logger.info("  Printer name : %s", self.config["printer_name"])
        self.logger.info("  Serial number: %s", self.config["serial"])
        self.logger.info("  Model        : %s", self.config["model"])
        self.logger.info("  Serial port  : %s @ %d baud", self.config["serial_port"], self.config["baudrate"])
        self.logger.info("  Access code  : %s", self.config["access_code"])
        self.logger.info("  Data dir     : %s", self._data_dir)
        self.logger.info("-" * 60)
        self.logger.info("Services:")
        self.logger.info("  SSDP   : port 2021 (printer discovery)")
        self.logger.info("  MQTT   : port 8883 (slicer communication, TLS)")
        self.logger.info("  FTP    : port 9990 (file upload, implicit FTPS)")
        self.logger.info("  Bind   : port 3000 (slicer handshake)")
        if connected:
            fw = self._middleware._serial.state.get("firmware_name", "Unknown")
            self.logger.info("  Printer: connected (%s)", fw)
        else:
            self.logger.info("  Printer: NOT connected")
        self.logger.info("-" * 60)
        self.logger.info("Ready! Add this printer in Bambu Studio or OrcaSlicer.")
        self.logger.info("=" * 60)

    async def stop(self) -> None:
        """Stop all middleware services."""
        self.logger.info("Stopping middleware services...")
        self._running = False

        if self._middleware:
            await self._middleware.stop()

        if self._ftp:
            await self._ftp.stop()
        if self._mqtt:
            await self._mqtt.stop()
        if self._ssdp:
            await self._ssdp.stop()
        if self._bind:
            await self._bind.stop()

        for task in self._tasks:
            task.cancel()

        if self._tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._tasks, return_exceptions=True),
                    timeout=2.0,
                )
            except TimeoutError:
                pass

        self._tasks = []
        self.logger.info("Middleware stopped")

    async def _on_file_received(self, file_path: Path, source_ip: str) -> None:
        """Handle file upload completion from FTP."""
        self.logger.info("Received file: %s from %s", file_path.name, source_ip)

    async def _on_print_command(self, filename: str, data: dict) -> None:
        """Handle print command from MQTT (project_file)."""
        self.logger.info("Print command for: %s", filename)
        if self._middleware:
            await self._middleware.handle_mqtt_command({"print": data})

    async def _on_mqtt_command(self, data: dict) -> None:
        """Handle any MQTT command - forward to middleware for G-code translation."""
        if self._middleware:
            await self._middleware.handle_mqtt_command(data)


# ============================================================================
# CLI and entry point
# ============================================================================

def load_config(args: argparse.Namespace) -> dict:
    """Build configuration from defaults, config file, and CLI args."""
    config = dict(DEFAULT_CONFIG)

    # Load config file if specified
    if args.config and Path(args.config).exists():
        with open(args.config) as f:
            file_config = json.load(f)
        config.update(file_config)

    # CLI args override config file
    if args.serial_port:
        config["serial_port"] = args.serial_port
    if args.baudrate:
        config["baudrate"] = args.baudrate
    if args.access_code:
        config["access_code"] = args.access_code
    if args.printer_name:
        config["printer_name"] = args.printer_name
    if args.serial:
        config["serial"] = args.serial
    if args.model:
        config["model"] = args.model
    if args.data_dir:
        config["data_dir"] = args.data_dir
    if args.log_level:
        config["log_level"] = args.log_level

    # Environment variables override everything
    config["serial_port"] = os.environ.get("MIDDLEWARE_SERIAL_PORT", config["serial_port"])
    config["baudrate"] = int(os.environ.get("MIDDLEWARE_BAUDRATE", config["baudrate"]))
    config["access_code"] = os.environ.get("MIDDLEWARE_ACCESS_CODE", config["access_code"])
    config["data_dir"] = os.environ.get("MIDDLEWARE_DATA_DIR", config["data_dir"])

    return config


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Bambuddy Middleware - Make any 3D printer work with BambuLab software",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s
  %(prog)s --serial-port /dev/ttyACM0 --baudrate 250000
  %(prog)s --access-code mycode123 --printer-name "My Printer"
  %(prog)s --config /etc/bambuddy-middleware/config.json
  %(prog)s --list-ports

Environment variables:
  MIDDLEWARE_SERIAL_PORT   Serial port path
  MIDDLEWARE_BAUDRATE      Baud rate
  MIDDLEWARE_ACCESS_CODE   Access code for slicer auth
  MIDDLEWARE_DATA_DIR      Data directory path
""",
    )

    parser.add_argument(
        "--serial-port", "-p",
        help="Serial port for 3D printer (default: /dev/ttyUSB0)",
    )
    parser.add_argument(
        "--baudrate", "-b",
        type=int,
        help="Baud rate (default: 115200)",
    )
    parser.add_argument(
        "--access-code", "-a",
        help="Access code for slicer authentication (default: 12345678)",
    )
    parser.add_argument(
        "--printer-name", "-n",
        help="Printer name shown in slicer (default: Bambuddy Middleware)",
    )
    parser.add_argument(
        "--serial", "-s",
        help="Printer serial number (default: auto-generated)",
    )
    parser.add_argument(
        "--model", "-m",
        help="Printer model code (default: 3DPrinter-X1-Carbon)",
    )
    parser.add_argument(
        "--data-dir", "-d",
        help="Data directory for certs and uploads (default: /var/lib/bambuddy-middleware)",
    )
    parser.add_argument(
        "--config", "-c",
        help="Path to JSON config file",
    )
    parser.add_argument(
        "--log-level", "-l",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Log level (default: INFO)",
    )
    parser.add_argument(
        "--list-ports",
        action="store_true",
        help="List available serial ports and exit",
    )

    return parser.parse_args()


async def main() -> None:
    """Main entry point."""
    args = parse_args()

    # Handle --list-ports
    if args.list_ports:
        ports = SerialConnection.list_serial_ports()
        if ports:
            print("Available serial ports:")
            for p in ports:
                print(f"  {p['port']:20s}  {p['description']}")
        else:
            print("No serial ports found.")
            print("Make sure your 3D printer is connected via USB.")
        return

    # Load configuration
    config = load_config(args)

    # Setup logging
    setup_logging(config["log_level"])

    # Create and start the service
    service = MiddlewareService(config)

    # Handle shutdown signals
    loop = asyncio.get_event_loop()
    stop_event = asyncio.Event()

    def signal_handler():
        logging.getLogger("middleware").info("Shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)

    try:
        await service.start()

        # Wait until shutdown signal
        await stop_event.wait()

    except KeyboardInterrupt:
        pass
    except Exception as e:
        logging.getLogger("middleware").error("Fatal error: %s", e)
        raise
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
