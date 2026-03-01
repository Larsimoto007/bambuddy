"""G-code middleware - bridges BambuLab MQTT protocol with serial 3D printers.

Translates between BambuLab's MQTT-based protocol (used by BamBuddy/slicers)
and standard G-code commands sent over a USB/serial connection to a physical
3D printer. Runs on a Raspberry Pi as middleware, making any G-code printer
appear as a BambuLab printer to the management software.

Supported features:
- Temperature monitoring and control (nozzle, bed, chamber)
- Fan control (part cooling, auxiliary, chamber)
- Print speed control
- Print lifecycle (start, pause, resume, stop)
- Real-time progress tracking
- G-code file extraction from 3MF and line-by-line sending
- Chamber light control
- Position reporting
"""

import asyncio
import logging
import re
import zipfile
from pathlib import Path

from backend.app.services.virtual_printer.serial_connection import SerialConnection

logger = logging.getLogger(__name__)

# BambuLab speed level to percentage mapping
SPEED_LEVEL_MAP = {
    1: 50,  # Silent
    2: 100,  # Normal
    3: 125,  # Sport
    4: 166,  # Ludicrous
}

# Reverse mapping for reporting
SPEED_PERCENT_TO_LEVEL = {
    50: 1,
    100: 2,
    125: 3,
    166: 4,
}


def _percent_to_level(percent: int) -> int:
    """Convert speed percentage to BambuLab speed level."""
    if percent <= 50:
        return 1
    if percent <= 100:
        return 2
    if percent <= 125:
        return 3
    return 4


class GCodeMiddleware:
    """Middleware translating BambuLab MQTT commands to/from serial G-code.

    Acts as a bridge between the virtual printer's MQTT server and a
    physical 3D printer connected via USB/serial cable.
    """

    def __init__(
        self,
        serial_port: str = "/dev/ttyUSB0",
        baudrate: int = 115200,
        upload_dir: Path | None = None,
    ):
        """Initialize the G-code middleware.

        Args:
            serial_port: Serial port for the physical printer
            baudrate: Baud rate for serial communication
            upload_dir: Directory where uploaded files are stored
        """
        self._serial = SerialConnection(
            port=serial_port,
            baudrate=baudrate,
        )
        self._upload_dir = upload_dir

        self._running = False
        self._poll_task: asyncio.Task | None = None
        self._print_task: asyncio.Task | None = None

        # Print state tracking
        self._gcode_state = "IDLE"  # IDLE, PREPARE, RUNNING, PAUSE, FINISH, FAILED
        self._current_file = ""
        self._progress = 0
        self._layer_num = 0
        self._total_layers = 0
        self._remaining_time = 0
        self._print_start_time = 0.0

        # G-code file sending state
        self._gcode_lines: list[str] = []
        self._current_line = 0
        self._paused = False

        # Fan state tracking (0-100 scale for BambuLab)
        self._part_fan_speed = 0
        self._aux_fan_speed = 0
        self._chamber_fan_speed = 0

        # Light state
        self._chamber_light = True

        # Status callback (set by manager)
        self.on_status_update = None

    @property
    def is_connected(self) -> bool:
        """Check if the serial printer is connected."""
        return self._serial.is_connected

    @property
    def gcode_state(self) -> str:
        """Get current G-code state in BambuLab format."""
        return self._gcode_state

    @property
    def serial_port(self) -> str:
        """Get configured serial port."""
        return self._serial.port

    @property
    def baudrate(self) -> int:
        """Get configured baud rate."""
        return self._serial.baudrate

    async def start(self) -> bool:
        """Start the middleware - connect to printer and begin polling.

        Returns:
            True if connected successfully
        """
        if self._running:
            return True

        connected = await self._serial.connect()
        if not connected:
            return False

        self._running = True

        # Start periodic status polling
        self._poll_task = asyncio.create_task(self._poll_printer_status())

        logger.info(
            "G-code middleware started (port=%s, baud=%d, firmware=%s)",
            self._serial.port,
            self._serial.baudrate,
            self._serial.state.get("firmware_name", "Unknown"),
        )
        return True

    async def stop(self) -> None:
        """Stop the middleware and disconnect from printer."""
        self._running = False
        self._paused = False

        # Cancel print if in progress
        if self._print_task and not self._print_task.done():
            self._print_task.cancel()
            try:
                await self._print_task
            except asyncio.CancelledError:
                pass
            self._print_task = None

        # Stop polling
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        await self._serial.disconnect()
        self._gcode_state = "IDLE"
        logger.info("G-code middleware stopped")

    async def _poll_printer_status(self) -> None:
        """Periodically poll printer for temperature and progress."""
        logger.info("Starting printer status polling")

        while self._running:
            try:
                # Query temperatures (every cycle)
                await self._serial.query_temperatures()

                # Query progress if printing
                if self._gcode_state == "RUNNING":
                    await self._serial.query_sd_progress()

                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Status poll error: %s", e)
                await asyncio.sleep(5.0)

        logger.info("Printer status polling stopped")

    def get_bambu_status(self) -> dict:
        """Build a BambuLab-format status report from current printer state.

        Returns:
            Dict matching BambuLab MQTT status message format
        """
        serial_state = self._serial.state

        # Calculate speed level from percentage
        speed_percent = serial_state.get("speed_factor", 100)
        speed_level = _percent_to_level(speed_percent)

        # Fan speeds: convert 0-100 to string format used by BambuLab
        # BambuLab uses "0"-"15" scale (mapped from 0-255 PWM)
        def fan_to_bambu(percent: int) -> str:
            return str(int(percent * 15 / 100))

        return {
            "print": {
                "command": "push_status",
                "msg": 0,
                "gcode_state": self._gcode_state,
                "gcode_file": self._current_file,
                "subtask_name": self._current_file.replace(".gcode", "").replace(".3mf", "")
                if self._current_file
                else "",
                "mc_percent": self._progress,
                "mc_remaining_time": self._remaining_time,
                "layer_num": self._layer_num,
                "total_layer_num": self._total_layers,
                "wifi_signal": "-44dBm",
                "print_error": 0,
                "bed_temper": serial_state.get("bed_temp", 0.0),
                "bed_target_temper": serial_state.get("bed_target", 0.0),
                "nozzle_temper": serial_state.get("nozzle_temp", 0.0),
                "nozzle_target_temper": serial_state.get("nozzle_target", 0.0),
                "chamber_temper": serial_state.get("chamber_temp", 0.0),
                "cooling_fan_speed": fan_to_bambu(self._part_fan_speed),
                "big_fan1_speed": fan_to_bambu(self._aux_fan_speed),
                "big_fan2_speed": fan_to_bambu(self._chamber_fan_speed),
                "heatbreak_fan_speed": "0",
                "spd_lvl": speed_level,
                "spd_mag": speed_percent,
                "stg": [],
                "stg_cur": 0 if self._gcode_state == "RUNNING" else -1,
                "home_flag": 256,
                "hw_switch_state": 0,
                "sdcard": True,
                "lights_report": [{"node": "chamber_light", "mode": "on" if self._chamber_light else "off"}],
                "nozzle_diameter": "0.4",
                "nozzle_type": "stainless_steel",
                "online": {"ahb": False, "rfid": False, "version": 7},
                "ipcam": {
                    "ipcam_dev": "1",
                    "ipcam_record": "enable",
                    "timelapse": "disable",
                    "resolution": "1080p",
                    "mode_bits": 0,
                },
                "xcam": {
                    "allow_skip_parts": False,
                    "buildplate_marker_detector": False,
                    "first_layer_inspector": False,
                    "halt_print_sensitivity": "medium",
                    "print_halt": False,
                    "printing_monitor": False,
                    "spaghetti_detector": False,
                },
            }
        }

    # ========================================================================
    # BambuLab command handlers (MQTT → G-code translation)
    # ========================================================================

    async def handle_mqtt_command(self, data: dict) -> None:
        """Handle a BambuLab MQTT command and translate to G-code.

        Args:
            data: Parsed JSON from MQTT device/{serial}/request topic
        """
        if "print" in data:
            await self._handle_print_command(data["print"])

        if "system" in data:
            await self._handle_system_command(data["system"])

    async def _handle_print_command(self, print_data: dict) -> None:
        """Handle BambuLab print commands.

        Args:
            print_data: The 'print' section of the MQTT message
        """
        command = print_data.get("command", "")
        logger.info("Middleware handling print command: %s", command)

        if command == "project_file":
            # Start a new print from uploaded file
            filename = print_data.get("file", "") or print_data.get("subtask_name", "")
            await self._start_print_from_file(filename, print_data)

        elif command == "pause":
            await self.pause_print()

        elif command == "resume":
            await self.resume_print()

        elif command == "stop":
            await self.stop_print()

        elif command == "gcode_line":
            # Direct G-code execution
            gcode = print_data.get("param", "")
            if gcode:
                await self._serial.send_command(gcode)

        elif command == "set_bed_temperature" or "bed_target_temper" in print_data:
            temp = print_data.get("bed_target_temper", print_data.get("param", 0))
            await self.set_bed_temperature(int(temp))

        elif command == "set_nozzle_temperature" or "nozzle_target_temper" in print_data:
            temp = print_data.get("nozzle_target_temper", print_data.get("param", 0))
            await self.set_nozzle_temperature(int(temp))

        elif command == "set_chamber_temperature" or "chamber_target_temper" in print_data:
            temp = print_data.get("chamber_target_temper", print_data.get("param", 0))
            await self.set_chamber_temperature(int(temp))

        elif command == "set_print_speed" or "spd_lvl" in print_data:
            level = print_data.get("spd_lvl", print_data.get("param", 2))
            await self.set_speed_level(int(level))

        elif command == "set_part_fan" or "cooling_fan_speed" in print_data:
            speed = print_data.get("cooling_fan_speed", print_data.get("param", 0))
            await self.set_part_fan(int(speed))

        elif command == "set_aux_fan" or "big_fan1_speed" in print_data:
            speed = print_data.get("big_fan1_speed", print_data.get("param", 0))
            await self.set_aux_fan(int(speed))

        elif command == "set_chamber_fan" or "big_fan2_speed" in print_data:
            speed = print_data.get("big_fan2_speed", print_data.get("param", 0))
            await self.set_chamber_fan(int(speed))

        elif command == "set_chamber_light" or "lights_report" in print_data:
            lights = print_data.get("lights_report", [])
            if lights:
                mode = lights[0].get("mode", "on")
                await self.set_chamber_light(mode == "on")
            else:
                param = print_data.get("param", "on")
                await self.set_chamber_light(param == "on")

    async def _handle_system_command(self, system_data: dict) -> None:
        """Handle BambuLab system commands.

        Args:
            system_data: The 'system' section of the MQTT message
        """
        command = system_data.get("command", "")
        logger.info("Middleware handling system command: %s", command)

        if command == "gcode_line":
            gcode = system_data.get("param", "")
            if gcode:
                await self._serial.send_command(gcode)

    # ========================================================================
    # Temperature control
    # ========================================================================

    async def set_bed_temperature(self, temperature: int) -> None:
        """Set bed temperature.

        Args:
            temperature: Target temperature in °C
        """
        await self._serial.send_command(f"M140 S{temperature}")
        logger.info("Set bed temperature to %d°C", temperature)

    async def set_nozzle_temperature(self, temperature: int) -> None:
        """Set nozzle/hotend temperature.

        Args:
            temperature: Target temperature in °C
        """
        await self._serial.send_command(f"M104 S{temperature}")
        logger.info("Set nozzle temperature to %d°C", temperature)

    async def set_chamber_temperature(self, temperature: int) -> None:
        """Set chamber temperature (if supported by printer).

        Args:
            temperature: Target temperature in °C
        """
        await self._serial.send_command(f"M141 S{temperature}")
        logger.info("Set chamber temperature to %d°C", temperature)

    # ========================================================================
    # Fan control
    # ========================================================================

    async def set_part_fan(self, speed: int) -> None:
        """Set part cooling fan speed.

        Args:
            speed: Fan speed 0-100 (percentage) or 0-15 (BambuLab scale)
        """
        # BambuLab uses 0-15 scale, convert to 0-255 PWM
        if speed <= 15:
            pwm = int(speed * 255 / 15)
            self._part_fan_speed = int(speed * 100 / 15)
        else:
            pwm = int(speed * 255 / 100)
            self._part_fan_speed = speed

        if pwm == 0:
            await self._serial.send_command("M107")
        else:
            await self._serial.send_command(f"M106 S{pwm}")
        logger.info("Set part fan to %d (PWM %d)", speed, pwm)

    async def set_aux_fan(self, speed: int) -> None:
        """Set auxiliary fan speed.

        Args:
            speed: Fan speed 0-100 (percentage) or 0-15 (BambuLab scale)
        """
        if speed <= 15:
            pwm = int(speed * 255 / 15)
            self._aux_fan_speed = int(speed * 100 / 15)
        else:
            pwm = int(speed * 255 / 100)
            self._aux_fan_speed = speed

        await self._serial.send_command(f"M106 P1 S{pwm}")
        logger.info("Set aux fan to %d (PWM %d)", speed, pwm)

    async def set_chamber_fan(self, speed: int) -> None:
        """Set chamber fan speed.

        Args:
            speed: Fan speed 0-100 (percentage) or 0-15 (BambuLab scale)
        """
        if speed <= 15:
            pwm = int(speed * 255 / 15)
            self._chamber_fan_speed = int(speed * 100 / 15)
        else:
            pwm = int(speed * 255 / 100)
            self._chamber_fan_speed = speed

        await self._serial.send_command(f"M106 P2 S{pwm}")
        logger.info("Set chamber fan to %d (PWM %d)", speed, pwm)

    # ========================================================================
    # Speed control
    # ========================================================================

    async def set_speed_level(self, level: int) -> None:
        """Set print speed level (BambuLab scale 1-4).

        Args:
            level: Speed level (1=Silent, 2=Normal, 3=Sport, 4=Ludicrous)
        """
        percent = SPEED_LEVEL_MAP.get(level, 100)
        await self._serial.send_command(f"M220 S{percent}")
        logger.info("Set speed level %d (%d%%)", level, percent)

    # ========================================================================
    # Light control
    # ========================================================================

    async def set_chamber_light(self, on: bool) -> None:
        """Set chamber light on/off.

        Args:
            on: True for on, False for off
        """
        self._chamber_light = on
        await self._serial.send_command(f"M355 S{1 if on else 0}")
        logger.info("Set chamber light %s", "on" if on else "off")

    # ========================================================================
    # Print control
    # ========================================================================

    async def pause_print(self) -> None:
        """Pause the current print."""
        if self._gcode_state != "RUNNING":
            return

        self._paused = True
        self._gcode_state = "PAUSE"
        await self._serial.send_command("M25")  # Pause SD print
        logger.info("Print paused")

    async def resume_print(self) -> None:
        """Resume a paused print."""
        if self._gcode_state != "PAUSE":
            return

        self._paused = False
        self._gcode_state = "RUNNING"
        await self._serial.send_command("M24")  # Resume SD print
        logger.info("Print resumed")

    async def stop_print(self) -> None:
        """Stop/abort the current print."""
        if self._gcode_state not in ("RUNNING", "PAUSE", "PREPARE"):
            return

        self._paused = False
        self._gcode_state = "IDLE"
        self._progress = 0
        self._layer_num = 0

        # Cancel line-by-line sending
        if self._print_task and not self._print_task.done():
            self._print_task.cancel()
            try:
                await self._print_task
            except asyncio.CancelledError:
                pass
            self._print_task = None

        # Send abort commands
        await self._serial.send_command("M524", wait_for_ok=False)  # Abort SD print
        await asyncio.sleep(0.5)
        # Turn off heaters and fans for safety
        await self._serial.send_command("M104 S0")  # Nozzle off
        await self._serial.send_command("M140 S0")  # Bed off
        await self._serial.send_command("M107")  # Fans off

        logger.info("Print stopped")

    async def _start_print_from_file(self, filename: str, print_data: dict) -> None:
        """Start printing a file.

        Handles both direct G-code files and 3MF files (extracts G-code).

        Args:
            filename: Name of the file to print
            print_data: Full print command data
        """
        self._current_file = filename
        self._gcode_state = "PREPARE"
        self._progress = 0
        self._layer_num = 0
        self._total_layers = 0

        logger.info("Preparing to print: %s", filename)

        # Find the file in upload directory
        if self._upload_dir:
            upload_dir = self._upload_dir
        else:
            from backend.app.core.config import settings as app_settings

            upload_dir = app_settings.base_dir / "virtual_printer" / "uploads"

        file_path = upload_dir / filename
        if not file_path.exists():
            # Try with cache subdirectory
            file_path = upload_dir / "cache" / filename
            if not file_path.exists():
                logger.error("Print file not found: %s", filename)
                self._gcode_state = "FAILED"
                return

        # Extract G-code
        gcode_content = self._extract_gcode(file_path)
        if not gcode_content:
            logger.error("Failed to extract G-code from: %s", filename)
            self._gcode_state = "FAILED"
            return

        # Parse G-code for metadata
        self._gcode_lines = [
            line.strip() for line in gcode_content.split("\n") if line.strip() and not line.strip().startswith(";")
        ]
        self._total_layers = self._count_layers(gcode_content)
        self._current_line = 0
        self._paused = False

        logger.info(
            "Loaded %d G-code lines, %d layers from %s",
            len(self._gcode_lines),
            self._total_layers,
            filename,
        )

        # Start sending G-code in background
        self._gcode_state = "RUNNING"
        self._print_task = asyncio.create_task(self._send_gcode_lines())

    def _extract_gcode(self, file_path: Path) -> str:
        """Extract G-code content from a file (3MF or plain G-code).

        Args:
            file_path: Path to the file

        Returns:
            G-code content string, or empty string on failure
        """
        suffix = file_path.suffix.lower()

        if suffix == ".gcode" or suffix == ".g":
            try:
                return file_path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                logger.error("Error reading G-code file: %s", e)
                return ""

        if suffix == ".3mf":
            return self._extract_gcode_from_3mf(file_path)

        logger.warning("Unsupported file format: %s", suffix)
        return ""

    @staticmethod
    def _extract_gcode_from_3mf(file_path: Path) -> str:
        """Extract G-code from a 3MF file.

        BambuLab 3MF files contain G-code in Metadata/plate_X.gcode files.

        Args:
            file_path: Path to the 3MF file

        Returns:
            G-code content string
        """
        try:
            with zipfile.ZipFile(file_path, "r") as zf:
                # Look for G-code files in the 3MF archive
                gcode_files = [
                    n for n in zf.namelist() if n.endswith(".gcode") and ("Metadata/" in n or "metadata/" in n)
                ]

                if not gcode_files:
                    # Fallback: look for any .gcode file
                    gcode_files = [n for n in zf.namelist() if n.endswith(".gcode")]

                if gcode_files:
                    # Use first plate by default
                    gcode_file = sorted(gcode_files)[0]
                    logger.info("Extracting G-code from 3MF: %s", gcode_file)
                    return zf.read(gcode_file).decode("utf-8", errors="replace")

                logger.warning("No G-code found in 3MF file: %s", file_path.name)
                return ""

        except (zipfile.BadZipFile, OSError) as e:
            logger.error("Error reading 3MF file %s: %s", file_path.name, e)
            return ""

    @staticmethod
    def _count_layers(gcode_content: str) -> int:
        """Count total layers in G-code content.

        Looks for layer change markers in comments.

        Args:
            gcode_content: Raw G-code text

        Returns:
            Estimated total number of layers
        """
        # Common layer change patterns from various slicers
        layer_pattern = re.compile(r";LAYER:(\d+)|;LAYER_CHANGE|; layer (\d+)|;Z:[\d.]+")
        max_layer = 0
        layer_count = 0

        for match in layer_pattern.finditer(gcode_content):
            layer_count += 1
            if match.group(1):
                max_layer = max(max_layer, int(match.group(1)))
            elif match.group(2):
                max_layer = max(max_layer, int(match.group(2)))

        return max(max_layer + 1, layer_count) if layer_count > 0 else 0

    async def _send_gcode_lines(self) -> None:
        """Send G-code lines to printer one by one."""
        total = len(self._gcode_lines)
        logger.info("Starting G-code send: %d lines", total)

        # Layer tracking pattern
        layer_pattern = re.compile(r";LAYER:(\d+)|;LAYER_CHANGE|; layer (\d+)")

        try:
            while self._current_line < total and self._running:
                if self._paused:
                    await asyncio.sleep(0.1)
                    continue

                line = self._gcode_lines[self._current_line]

                # Track layer changes (even in comment lines we stripped earlier,
                # re-check against original lines if needed)
                layer_match = layer_pattern.search(line)
                if layer_match:
                    if layer_match.group(1):
                        self._layer_num = int(layer_match.group(1))
                    else:
                        self._layer_num += 1

                # Skip empty lines and pure comments
                if not line or line.startswith(";"):
                    self._current_line += 1
                    continue

                # Strip inline comments
                if ";" in line:
                    line = line[: line.index(";")].strip()

                if line:
                    await self._serial.send_command(line)

                self._current_line += 1

                # Update progress
                self._progress = int((self._current_line / total) * 100)

                # Small delay between commands for flow control
                # Actual pacing is handled by waiting for 'ok' from printer
                await asyncio.sleep(0.01)

            # Print complete
            if self._current_line >= total:
                self._gcode_state = "FINISH"
                self._progress = 100
                logger.info("Print complete: %s", self._current_file)

        except asyncio.CancelledError:
            logger.info("G-code sending cancelled")
        except Exception as e:
            logger.error("Error during G-code send: %s", e)
            self._gcode_state = "FAILED"

    def get_status(self) -> dict:
        """Get middleware status summary.

        Returns:
            Status dict for API response
        """
        return {
            "connected": self.is_connected,
            "serial_port": self._serial.port,
            "baudrate": self._serial.baudrate,
            "firmware": self._serial.state.get("firmware_name", ""),
            "gcode_state": self._gcode_state,
            "current_file": self._current_file,
            "progress": self._progress,
            "layer_num": self._layer_num,
            "total_layers": self._total_layers,
            "nozzle_temp": self._serial.state.get("nozzle_temp", 0.0),
            "bed_temp": self._serial.state.get("bed_temp", 0.0),
        }
