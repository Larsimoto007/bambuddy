"""Unit tests for G-code middleware and serial connection.

Tests the serial connection parsing, middleware command translation,
and BambuLab protocol bridging without requiring a physical printer.
"""

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


class TestSerialConnection:
    """Tests for SerialConnection class."""

    @pytest.fixture
    def connection(self):
        """Create a SerialConnection instance (not connected)."""
        from backend.app.services.virtual_printer.serial_connection import SerialConnection

        return SerialConnection(port="/dev/ttyUSB0", baudrate=115200)

    # ========================================================================
    # Tests for temperature parsing
    # ========================================================================

    def test_parse_temperatures_full(self, connection):
        """Verify parsing of full M105 response with nozzle, bed, chamber."""
        connection._parse_temperatures("ok T:220.5 /220.0 B:60.3 /60.0 C:45.0 /0.0")

        assert connection.state["nozzle_temp"] == 220.5
        assert connection.state["nozzle_target"] == 220.0
        assert connection.state["bed_temp"] == 60.3
        assert connection.state["bed_target"] == 60.0
        assert connection.state["chamber_temp"] == 45.0
        assert connection.state["chamber_target"] == 0.0

    def test_parse_temperatures_nozzle_and_bed(self, connection):
        """Verify parsing of M105 response with nozzle and bed only."""
        connection._parse_temperatures("ok T:200.0 /210.0 B:55.0 /60.0")

        assert connection.state["nozzle_temp"] == 200.0
        assert connection.state["nozzle_target"] == 210.0
        assert connection.state["bed_temp"] == 55.0
        assert connection.state["bed_target"] == 60.0
        assert connection.state["chamber_temp"] == 0.0  # Unchanged

    def test_parse_temperatures_nozzle_only(self, connection):
        """Verify parsing of M105 response with nozzle only."""
        connection._parse_temperatures("ok T:180.0 /190.0")

        assert connection.state["nozzle_temp"] == 180.0
        assert connection.state["nozzle_target"] == 190.0

    def test_parse_temperatures_no_match(self, connection):
        """Verify no crash on non-matching response."""
        connection._parse_temperatures("echo: busy processing")

        assert connection.state["nozzle_temp"] == 0.0

    # ========================================================================
    # Tests for position parsing
    # ========================================================================

    def test_parse_position_full(self, connection):
        """Verify parsing of M114 position response."""
        connection._parse_position("X:100.00 Y:200.00 Z:10.50 E:500.00 Count X:8000 Y:16000 Z:4200")

        assert connection.state["x_pos"] == 100.0
        assert connection.state["y_pos"] == 200.0
        assert connection.state["z_pos"] == 10.5
        assert connection.state["e_pos"] == 500.0

    def test_parse_position_without_extruder(self, connection):
        """Verify parsing of position without E axis."""
        connection._parse_position("X:50.00 Y:75.00 Z:5.00")

        assert connection.state["x_pos"] == 50.0
        assert connection.state["y_pos"] == 75.0
        assert connection.state["z_pos"] == 5.0

    # ========================================================================
    # Tests for SD progress parsing
    # ========================================================================

    def test_parse_sd_progress_byte_format(self, connection):
        """Verify parsing of SD progress in byte format."""
        connection._parse_sd_progress("SD printing byte 2500/10000")

        assert connection.state["sd_progress"] == 25
        assert connection.state["sd_printing"] is True

    def test_parse_sd_progress_not_printing(self, connection):
        """Verify parsing when not SD printing."""
        connection._parse_sd_progress("Not SD printing")

        assert connection.state["sd_printing"] is False

    def test_parse_sd_progress_percentage(self, connection):
        """Verify parsing of percentage format."""
        connection._parse_sd_progress("50.0%")

        assert connection.state["sd_progress"] == 50
        assert connection.state["sd_printing"] is True

    def test_parse_sd_progress_zero_total(self, connection):
        """Verify handling of zero total bytes."""
        connection._parse_sd_progress("SD printing byte 0/0")

        assert connection.state["sd_progress"] == 0
        assert connection.state["sd_printing"] is False

    # ========================================================================
    # Tests for speed parsing
    # ========================================================================

    def test_parse_speed_factor(self, connection):
        """Verify parsing of M220 speed factor response."""
        connection._parse_speed_factor("FR:150%")

        assert connection.state["speed_factor"] == 150

    # ========================================================================
    # Tests for firmware parsing
    # ========================================================================

    def test_parse_firmware_info_marlin(self, connection):
        """Verify parsing of Marlin firmware info."""
        connection._parse_firmware_info("FIRMWARE_NAME:Marlin 2.1.2.1 SOURCE_CODE_URL:github.com/MarlinFirmware/Marlin")

        assert "Marlin" in connection.state["firmware_name"]

    def test_parse_firmware_info_klipper(self, connection):
        """Verify parsing of Klipper firmware info."""
        connection._parse_firmware_info("FIRMWARE_NAME:Klipper")

        assert "Klipper" in connection.state["firmware_name"]

    # ========================================================================
    # Tests for connection state
    # ========================================================================

    def test_initial_state_not_connected(self, connection):
        """Verify initial state is disconnected."""
        assert connection.is_connected is False
        assert connection.state["connected"] is False

    def test_list_serial_ports(self):
        """Verify list_serial_ports returns list."""
        from backend.app.services.virtual_printer.serial_connection import SerialConnection

        # Should return a list (may be empty if no ports available)
        ports = SerialConnection.list_serial_ports()
        assert isinstance(ports, list)


class TestGCodeMiddleware:
    """Tests for GCodeMiddleware class."""

    @pytest.fixture
    def middleware(self):
        """Create a GCodeMiddleware instance with mocked serial connection."""
        from backend.app.services.virtual_printer.gcode_middleware import GCodeMiddleware

        mw = GCodeMiddleware(serial_port="/dev/ttyUSB0", baudrate=115200)
        # Mock the serial connection to avoid real hardware
        mw._serial = MagicMock()
        mw._serial.is_connected = True
        mw._serial.port = "/dev/ttyUSB0"
        mw._serial.baudrate = 115200
        mw._serial.state = {
            "nozzle_temp": 200.0,
            "nozzle_target": 210.0,
            "bed_temp": 60.0,
            "bed_target": 60.0,
            "chamber_temp": 30.0,
            "chamber_target": 0.0,
            "speed_factor": 100,
            "firmware_name": "Marlin 2.1.2.1",
            "connected": True,
        }
        mw._serial.send_command = AsyncMock(return_value="ok")
        mw._serial.query_temperatures = AsyncMock()
        mw._serial.query_sd_progress = AsyncMock()
        return mw

    # ========================================================================
    # Tests for BambuLab status generation
    # ========================================================================

    def test_get_bambu_status_idle(self, middleware):
        """Verify BambuLab status in IDLE state."""
        middleware._gcode_state = "IDLE"

        status = middleware.get_bambu_status()

        assert "print" in status
        assert status["print"]["gcode_state"] == "IDLE"
        assert status["print"]["bed_temper"] == 60.0
        assert status["print"]["nozzle_temper"] == 200.0
        assert status["print"]["chamber_temper"] == 30.0
        assert status["print"]["mc_percent"] == 0

    def test_get_bambu_status_printing(self, middleware):
        """Verify BambuLab status during printing."""
        middleware._gcode_state = "RUNNING"
        middleware._progress = 45
        middleware._layer_num = 12
        middleware._total_layers = 100
        middleware._current_file = "test_print.3mf"

        status = middleware.get_bambu_status()

        assert status["print"]["gcode_state"] == "RUNNING"
        assert status["print"]["mc_percent"] == 45
        assert status["print"]["layer_num"] == 12
        assert status["print"]["total_layer_num"] == 100
        assert status["print"]["subtask_name"] == "test_print"

    def test_get_bambu_status_has_required_fields(self, middleware):
        """Verify all required BambuLab fields are present."""
        status = middleware.get_bambu_status()
        print_status = status["print"]

        required_fields = [
            "gcode_state",
            "bed_temper",
            "nozzle_temper",
            "cooling_fan_speed",
            "spd_lvl",
            "spd_mag",
            "lights_report",
            "ipcam",
            "xcam",
            "sdcard",
            "nozzle_diameter",
            "nozzle_type",
        ]
        for field in required_fields:
            assert field in print_status, f"Missing required field: {field}"

    # ========================================================================
    # Tests for temperature control
    # ========================================================================

    @pytest.mark.asyncio
    async def test_set_bed_temperature(self, middleware):
        """Verify bed temperature command sends correct G-code."""
        await middleware.set_bed_temperature(60)

        middleware._serial.send_command.assert_called_once_with("M140 S60")

    @pytest.mark.asyncio
    async def test_set_nozzle_temperature(self, middleware):
        """Verify nozzle temperature command sends correct G-code."""
        await middleware.set_nozzle_temperature(220)

        middleware._serial.send_command.assert_called_once_with("M104 S220")

    @pytest.mark.asyncio
    async def test_set_chamber_temperature(self, middleware):
        """Verify chamber temperature command sends correct G-code."""
        await middleware.set_chamber_temperature(45)

        middleware._serial.send_command.assert_called_once_with("M141 S45")

    # ========================================================================
    # Tests for fan control
    # ========================================================================

    @pytest.mark.asyncio
    async def test_set_part_fan_off(self, middleware):
        """Verify part fan off sends M107."""
        await middleware.set_part_fan(0)

        middleware._serial.send_command.assert_called_once_with("M107")

    @pytest.mark.asyncio
    async def test_set_part_fan_full(self, middleware):
        """Verify part fan full speed sends correct PWM."""
        await middleware.set_part_fan(15)  # BambuLab scale 0-15

        middleware._serial.send_command.assert_called_once_with("M106 S255")

    @pytest.mark.asyncio
    async def test_set_aux_fan(self, middleware):
        """Verify aux fan command targets fan index 1."""
        await middleware.set_aux_fan(8)  # BambuLab scale

        # Should target P1 (aux fan)
        args = middleware._serial.send_command.call_args[0][0]
        assert "M106 P1" in args

    @pytest.mark.asyncio
    async def test_set_chamber_fan(self, middleware):
        """Verify chamber fan command targets fan index 2."""
        await middleware.set_chamber_fan(10)

        args = middleware._serial.send_command.call_args[0][0]
        assert "M106 P2" in args

    # ========================================================================
    # Tests for speed control
    # ========================================================================

    @pytest.mark.asyncio
    async def test_set_speed_silent(self, middleware):
        """Verify speed level 1 (Silent) maps to 50%."""
        await middleware.set_speed_level(1)

        middleware._serial.send_command.assert_called_once_with("M220 S50")

    @pytest.mark.asyncio
    async def test_set_speed_normal(self, middleware):
        """Verify speed level 2 (Normal) maps to 100%."""
        await middleware.set_speed_level(2)

        middleware._serial.send_command.assert_called_once_with("M220 S100")

    @pytest.mark.asyncio
    async def test_set_speed_sport(self, middleware):
        """Verify speed level 3 (Sport) maps to 125%."""
        await middleware.set_speed_level(3)

        middleware._serial.send_command.assert_called_once_with("M220 S125")

    @pytest.mark.asyncio
    async def test_set_speed_ludicrous(self, middleware):
        """Verify speed level 4 (Ludicrous) maps to 166%."""
        await middleware.set_speed_level(4)

        middleware._serial.send_command.assert_called_once_with("M220 S166")

    # ========================================================================
    # Tests for light control
    # ========================================================================

    @pytest.mark.asyncio
    async def test_set_chamber_light_on(self, middleware):
        """Verify chamber light on sends M355 S1."""
        await middleware.set_chamber_light(True)

        middleware._serial.send_command.assert_called_once_with("M355 S1")
        assert middleware._chamber_light is True

    @pytest.mark.asyncio
    async def test_set_chamber_light_off(self, middleware):
        """Verify chamber light off sends M355 S0."""
        await middleware.set_chamber_light(False)

        middleware._serial.send_command.assert_called_once_with("M355 S0")
        assert middleware._chamber_light is False

    # ========================================================================
    # Tests for print control
    # ========================================================================

    @pytest.mark.asyncio
    async def test_pause_print(self, middleware):
        """Verify pause sends M25."""
        middleware._gcode_state = "RUNNING"

        await middleware.pause_print()

        middleware._serial.send_command.assert_called_once_with("M25")
        assert middleware._gcode_state == "PAUSE"

    @pytest.mark.asyncio
    async def test_pause_ignored_when_idle(self, middleware):
        """Verify pause is ignored when not printing."""
        middleware._gcode_state = "IDLE"

        await middleware.pause_print()

        middleware._serial.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_print(self, middleware):
        """Verify resume sends M24."""
        middleware._gcode_state = "PAUSE"

        await middleware.resume_print()

        middleware._serial.send_command.assert_called_once_with("M24")
        assert middleware._gcode_state == "RUNNING"

    @pytest.mark.asyncio
    async def test_resume_ignored_when_not_paused(self, middleware):
        """Verify resume is ignored when not paused."""
        middleware._gcode_state = "RUNNING"

        await middleware.resume_print()

        middleware._serial.send_command.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_print(self, middleware):
        """Verify stop sends abort and safety commands."""
        middleware._gcode_state = "RUNNING"

        await middleware.stop_print()

        assert middleware._gcode_state == "IDLE"
        assert middleware._progress == 0
        # Should have sent M524, M104 S0, M140 S0, M107
        assert middleware._serial.send_command.call_count >= 4

    @pytest.mark.asyncio
    async def test_stop_ignored_when_idle(self, middleware):
        """Verify stop is ignored when already idle."""
        middleware._gcode_state = "IDLE"

        await middleware.stop_print()

        middleware._serial.send_command.assert_not_called()

    # ========================================================================
    # Tests for MQTT command translation
    # ========================================================================

    @pytest.mark.asyncio
    async def test_handle_mqtt_pause_command(self, middleware):
        """Verify MQTT pause command is translated."""
        middleware._gcode_state = "RUNNING"

        await middleware.handle_mqtt_command({"print": {"command": "pause"}})

        middleware._serial.send_command.assert_called_once_with("M25")
        assert middleware._gcode_state == "PAUSE"

    @pytest.mark.asyncio
    async def test_handle_mqtt_resume_command(self, middleware):
        """Verify MQTT resume command is translated."""
        middleware._gcode_state = "PAUSE"

        await middleware.handle_mqtt_command({"print": {"command": "resume"}})

        middleware._serial.send_command.assert_called_once_with("M24")

    @pytest.mark.asyncio
    async def test_handle_mqtt_stop_command(self, middleware):
        """Verify MQTT stop command is translated."""
        middleware._gcode_state = "RUNNING"

        await middleware.handle_mqtt_command({"print": {"command": "stop"}})

        assert middleware._gcode_state == "IDLE"

    @pytest.mark.asyncio
    async def test_handle_mqtt_gcode_line(self, middleware):
        """Verify direct G-code forwarding."""
        await middleware.handle_mqtt_command({"print": {"command": "gcode_line", "param": "G28"}})

        middleware._serial.send_command.assert_called_once_with("G28")

    @pytest.mark.asyncio
    async def test_handle_mqtt_system_gcode(self, middleware):
        """Verify system G-code command forwarding."""
        await middleware.handle_mqtt_command({"system": {"command": "gcode_line", "param": "M503"}})

        middleware._serial.send_command.assert_called_once_with("M503")

    # ========================================================================
    # Tests for G-code file parsing
    # ========================================================================

    def test_count_layers_with_layer_markers(self, middleware):
        """Verify layer counting from G-code comments."""
        gcode = """;LAYER:0
G1 X10 Y10
;LAYER:1
G1 X20 Y20
;LAYER:2
G1 X30 Y30
"""
        count = middleware._count_layers(gcode)
        assert count == 3  # Layers 0, 1, 2

    def test_count_layers_with_layer_change(self, middleware):
        """Verify layer counting from LAYER_CHANGE markers."""
        gcode = """;LAYER_CHANGE
G1 Z0.3
;LAYER_CHANGE
G1 Z0.5
;LAYER_CHANGE
G1 Z0.7
"""
        count = middleware._count_layers(gcode)
        assert count == 3

    def test_count_layers_no_markers(self, middleware):
        """Verify zero layers when no markers present."""
        gcode = """G28
G1 X10 Y10
G1 X20 Y20
"""
        count = middleware._count_layers(gcode)
        assert count == 0

    def test_extract_gcode_from_plain_file(self, middleware, tmp_path):
        """Verify G-code extraction from plain .gcode file."""
        gcode_file = tmp_path / "test.gcode"
        gcode_file.write_text("G28\nG1 X10 Y10\nM104 S200\n")

        content = middleware._extract_gcode(gcode_file)

        assert "G28" in content
        assert "G1 X10 Y10" in content

    def test_extract_gcode_unsupported_format(self, middleware, tmp_path):
        """Verify empty return for unsupported file formats."""
        stl_file = tmp_path / "test.stl"
        stl_file.write_text("solid test")

        content = middleware._extract_gcode(stl_file)
        assert content == ""

    # ========================================================================
    # Tests for middleware status
    # ========================================================================

    def test_get_status(self, middleware):
        """Verify middleware status contains expected fields."""
        middleware._gcode_state = "RUNNING"
        middleware._progress = 50
        middleware._current_file = "test.gcode"

        status = middleware.get_status()

        assert status["connected"] is True
        assert status["serial_port"] == "/dev/ttyUSB0"
        assert status["baudrate"] == 115200
        assert status["gcode_state"] == "RUNNING"
        assert status["progress"] == 50
        assert status["current_file"] == "test.gcode"
        assert status["firmware"] == "Marlin 2.1.2.1"

    def test_properties(self, middleware):
        """Verify property accessors."""
        assert middleware.is_connected is True
        assert middleware.gcode_state == "IDLE"
        assert middleware.serial_port == "/dev/ttyUSB0"
        assert middleware.baudrate == 115200


class TestSpeedMapping:
    """Tests for speed level/percentage mapping."""

    def test_percent_to_level_silent(self):
        """Verify 50% maps to level 1 (Silent)."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(50) == 1

    def test_percent_to_level_normal(self):
        """Verify 100% maps to level 2 (Normal)."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(100) == 2

    def test_percent_to_level_sport(self):
        """Verify 125% maps to level 3 (Sport)."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(125) == 3

    def test_percent_to_level_ludicrous(self):
        """Verify 166% maps to level 4 (Ludicrous)."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(166) == 4

    def test_percent_to_level_low(self):
        """Verify very low percentage maps to Silent."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(25) == 1

    def test_percent_to_level_over_max(self):
        """Verify percentage over 125 maps to Ludicrous."""
        from backend.app.services.virtual_printer.gcode_middleware import _percent_to_level

        assert _percent_to_level(200) == 4


class TestManagerMiddlewareMode:
    """Tests for VirtualPrinterManager middleware mode."""

    @pytest.fixture
    def manager(self):
        """Create a VirtualPrinterManager instance."""
        from backend.app.services.virtual_printer.manager import VirtualPrinterManager

        return VirtualPrinterManager()

    @pytest.mark.asyncio
    async def test_configure_middleware_requires_access_code(self, manager):
        """Verify middleware mode requires access code."""
        with pytest.raises(ValueError, match="Access code is required"):
            await manager.configure(enabled=True, mode="middleware", serial_port="/dev/ttyUSB0")

    @pytest.mark.asyncio
    async def test_configure_middleware_requires_serial_port(self, manager):
        """Verify middleware mode requires serial port."""
        manager._serial_port = ""  # Reset default

        with pytest.raises(ValueError, match="Serial port is required"):
            await manager.configure(
                enabled=True,
                mode="middleware",
                access_code="12345678",
                serial_port="",
            )

    @pytest.mark.asyncio
    async def test_configure_middleware_sets_parameters(self, manager):
        """Verify middleware parameters are stored correctly."""
        manager._start = AsyncMock()

        await manager.configure(
            enabled=True,
            access_code="12345678",
            mode="middleware",
            serial_port="/dev/ttyACM0",
            baudrate=250000,
        )

        assert manager._mode == "middleware"
        assert manager._serial_port == "/dev/ttyACM0"
        assert manager._baudrate == 250000

    def test_get_status_middleware_mode(self, manager):
        """Verify status includes middleware info."""
        manager._enabled = True
        manager._mode = "middleware"
        manager._serial_port = "/dev/ttyUSB0"
        manager._baudrate = 115200
        manager._tasks = [MagicMock(done=MagicMock(return_value=False))]

        status = manager.get_status()

        assert status["mode"] == "middleware"
        assert status["serial_port"] == "/dev/ttyUSB0"
        assert status["baudrate"] == 115200

    @pytest.mark.asyncio
    async def test_on_print_command_forwards_to_middleware(self, manager):
        """Verify print commands are forwarded to middleware in middleware mode."""
        manager._mode = "middleware"
        mock_middleware = MagicMock()
        mock_middleware.handle_mqtt_command = AsyncMock()
        manager._middleware = mock_middleware

        await manager._on_print_command("test.3mf", {"command": "pause"})

        mock_middleware.handle_mqtt_command.assert_called_once_with({"print": {"command": "pause"}})
