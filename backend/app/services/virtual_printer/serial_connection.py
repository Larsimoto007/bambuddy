"""Serial port communication for G-code printers.

Handles USB/serial connection to physical 3D printers, sending G-code
commands and parsing responses (temperatures, positions, progress).
"""

import asyncio
import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


class SerialConnection:
    """Manages serial communication with a G-code based 3D printer.

    Connects to a printer via USB/serial, sends G-code commands,
    and parses responses for temperature, position, and progress data.
    """

    # Common baud rates for 3D printers
    COMMON_BAUD_RATES = [115200, 250000, 57600, 38400, 19200, 9600]

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        timeout: float = 5.0,
        on_status_update: Callable[[dict], None] | None = None,
    ):
        """Initialize serial connection.

        Args:
            port: Serial port path (e.g., /dev/ttyUSB0, /dev/ttyACM0, COM3)
            baudrate: Baud rate for serial communication
            timeout: Read timeout in seconds
            on_status_update: Callback for status updates from printer
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.on_status_update = on_status_update

        self._serial = None
        self._connected = False
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._running = False

        # Parsed printer state
        self.state: dict = {
            "nozzle_temp": 0.0,
            "nozzle_target": 0.0,
            "bed_temp": 0.0,
            "bed_target": 0.0,
            "chamber_temp": 0.0,
            "chamber_target": 0.0,
            "x_pos": 0.0,
            "y_pos": 0.0,
            "z_pos": 0.0,
            "e_pos": 0.0,
            "sd_progress": 0,
            "sd_printing": False,
            "speed_factor": 100,
            "flow_factor": 100,
            "fan_speed": 0,
            "firmware_name": "",
            "connected": False,
        }

        # Regex patterns for parsing G-code responses
        self._temp_pattern = re.compile(
            r"T:(\d+\.?\d*)\s*/(\d+\.?\d*)"
            r"(?:\s+B:(\d+\.?\d*)\s*/(\d+\.?\d*))?"
            r"(?:\s+C:(\d+\.?\d*)\s*/(\d+\.?\d*))?"
        )
        self._pos_pattern = re.compile(
            r"X:(-?\d+\.?\d*)\s+Y:(-?\d+\.?\d*)\s+Z:(-?\d+\.?\d*)"
            r"(?:\s+E:(-?\d+\.?\d*))?"
        )
        self._sd_pattern = re.compile(r"SD printing byte (\d+)/(\d+)")
        self._sd_progress_pattern = re.compile(r"(\d+\.?\d*)%")
        self._speed_pattern = re.compile(r"FR:(\d+)%")
        self._flow_pattern = re.compile(r"E\d?:(\d+)%|Flow:\s*(\d+)%")

    @property
    def is_connected(self) -> bool:
        """Check if serial connection is active."""
        return self._connected and self._serial is not None

    async def connect(self) -> bool:
        """Open serial connection to printer.

        Returns:
            True if connection successful
        """
        try:
            import serial
        except ImportError:
            logger.error("pyserial not installed. Run: pip install pyserial")
            return False

        try:
            async with self._lock:
                if self._connected:
                    return True

                logger.info("Connecting to printer on %s at %d baud", self.port, self.baudrate)

                # Open serial port in a thread to avoid blocking
                loop = asyncio.get_event_loop()
                self._serial = await loop.run_in_executor(
                    None,
                    lambda: serial.Serial(
                        port=self.port,
                        baudrate=self.baudrate,
                        timeout=self.timeout,
                        write_timeout=self.timeout,
                    ),
                )

                self._connected = True
                self._running = True
                self.state["connected"] = True

                # Wait for printer startup message
                await asyncio.sleep(2.0)

                # Flush any startup data
                if self._serial.in_waiting:
                    await loop.run_in_executor(None, self._serial.read, self._serial.in_waiting)

                # Query firmware info
                response = await self.send_command("M115")
                if response:
                    self._parse_firmware_info(response)

                logger.info("Connected to printer: %s", self.state.get("firmware_name", "Unknown"))
                return True

        except Exception as e:
            logger.error("Failed to connect to printer on %s: %s", self.port, e)
            self._connected = False
            self.state["connected"] = False
            return False

    async def disconnect(self) -> None:
        """Close serial connection."""
        self._running = False
        self._connected = False
        self.state["connected"] = False

        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None

        if self._serial:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._serial.close)
            except Exception as e:
                logger.debug("Error closing serial port: %s", e)
            self._serial = None

        logger.info("Disconnected from printer")

    async def send_command(self, command: str, wait_for_ok: bool = True) -> str:
        """Send a G-code command and optionally wait for response.

        Args:
            command: G-code command (e.g., 'M105', 'G28')
            wait_for_ok: Whether to wait for 'ok' response

        Returns:
            Response string from printer
        """
        if not self.is_connected:
            return ""

        async with self._lock:
            try:
                loop = asyncio.get_event_loop()

                # Send command with newline
                cmd = command.strip() + "\n"
                await loop.run_in_executor(None, self._serial.write, cmd.encode("utf-8"))
                await loop.run_in_executor(None, self._serial.flush)

                if not wait_for_ok:
                    return ""

                # Read response lines until 'ok' or timeout
                response_lines = []
                try:
                    for _ in range(50):  # Max lines to read
                        line = await asyncio.wait_for(
                            loop.run_in_executor(None, self._serial.readline),
                            timeout=self.timeout,
                        )
                        if not line:
                            break

                        decoded = line.decode("utf-8", errors="replace").strip()
                        if decoded:
                            response_lines.append(decoded)

                        if decoded.startswith("ok"):
                            break
                except TimeoutError:
                    logger.debug("Timeout waiting for response to %s", command)

                return "\n".join(response_lines)

            except Exception as e:
                logger.error("Error sending command '%s': %s", command, e)
                return ""

    async def query_temperatures(self) -> dict:
        """Query current temperatures via M105.

        Returns:
            Dict with nozzle_temp, nozzle_target, bed_temp, bed_target,
            chamber_temp, chamber_target
        """
        response = await self.send_command("M105")
        if response:
            self._parse_temperatures(response)
        return {
            "nozzle_temp": self.state["nozzle_temp"],
            "nozzle_target": self.state["nozzle_target"],
            "bed_temp": self.state["bed_temp"],
            "bed_target": self.state["bed_target"],
            "chamber_temp": self.state["chamber_temp"],
            "chamber_target": self.state["chamber_target"],
        }

    async def query_position(self) -> dict:
        """Query current position via M114.

        Returns:
            Dict with x, y, z, e positions
        """
        response = await self.send_command("M114")
        if response:
            self._parse_position(response)
        return {
            "x": self.state["x_pos"],
            "y": self.state["y_pos"],
            "z": self.state["z_pos"],
            "e": self.state["e_pos"],
        }

    async def query_sd_progress(self) -> dict:
        """Query SD card print progress via M27.

        Returns:
            Dict with progress percentage and printing state
        """
        response = await self.send_command("M27")
        if response:
            self._parse_sd_progress(response)
        return {
            "progress": self.state["sd_progress"],
            "printing": self.state["sd_printing"],
        }

    async def query_speed_factor(self) -> int:
        """Query speed factor via M220.

        Returns:
            Speed factor percentage
        """
        response = await self.send_command("M220")
        if response:
            self._parse_speed_factor(response)
        return self.state["speed_factor"]

    def _parse_temperatures(self, response: str) -> None:
        """Parse M105 temperature response.

        Expected format: T:220.0 /220.0 B:60.0 /60.0 C:45.0 /0.0
        """
        match = self._temp_pattern.search(response)
        if match:
            self.state["nozzle_temp"] = float(match.group(1))
            self.state["nozzle_target"] = float(match.group(2))
            if match.group(3):
                self.state["bed_temp"] = float(match.group(3))
                self.state["bed_target"] = float(match.group(4))
            if match.group(5):
                self.state["chamber_temp"] = float(match.group(5))
                self.state["chamber_target"] = float(match.group(6))

    def _parse_position(self, response: str) -> None:
        """Parse M114 position response.

        Expected format: X:100.00 Y:200.00 Z:10.00 E:500.00
        """
        match = self._pos_pattern.search(response)
        if match:
            self.state["x_pos"] = float(match.group(1))
            self.state["y_pos"] = float(match.group(2))
            self.state["z_pos"] = float(match.group(3))
            if match.group(4):
                self.state["e_pos"] = float(match.group(4))

    def _parse_sd_progress(self, response: str) -> None:
        """Parse M27 SD progress response.

        Expected format: SD printing byte 1234/5678
        Or: Not SD printing
        """
        if "Not SD printing" in response:
            self.state["sd_printing"] = False
            return

        match = self._sd_pattern.search(response)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self.state["sd_printing"] = total > 0
            self.state["sd_progress"] = int((current / total) * 100) if total > 0 else 0
            return

        # Alternative percentage format
        match = self._sd_progress_pattern.search(response)
        if match:
            self.state["sd_progress"] = int(float(match.group(1)))
            self.state["sd_printing"] = self.state["sd_progress"] > 0

    def _parse_speed_factor(self, response: str) -> None:
        """Parse M220 speed factor response.

        Expected format: FR:100%
        """
        match = self._speed_pattern.search(response)
        if match:
            self.state["speed_factor"] = int(match.group(1))

    def _parse_firmware_info(self, response: str) -> None:
        """Parse M115 firmware info response.

        Expected: FIRMWARE_NAME:Marlin ...
        """
        for line in response.split("\n"):
            if "FIRMWARE_NAME:" in line:
                # Extract firmware name
                start = line.index("FIRMWARE_NAME:") + len("FIRMWARE_NAME:")
                # Find end (next space or end of line)
                name = line[start:].split("SOURCE_CODE")[0].strip()
                self.state["firmware_name"] = name
                break

    @staticmethod
    def list_serial_ports() -> list[dict]:
        """List available serial ports.

        Returns:
            List of dicts with port, description, and hardware_id
        """
        try:
            from serial.tools.list_ports import comports

            return [
                {
                    "port": p.device,
                    "description": p.description,
                    "hardware_id": p.hwid,
                }
                for p in comports()
            ]
        except ImportError:
            logger.error("pyserial not installed")
            return []
