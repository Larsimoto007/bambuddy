"""MQTT server for the middleware.

Implements a minimal MQTT broker that accepts connections from BambuLab
slicers, authenticates them, receives print commands, and pushes status
reports. Uses raw sockets (no external MQTT library needed).

Standalone module - no external project dependencies.
"""

import asyncio
import json
import logging
import ssl
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

MQTT_PORT = 8883


class MQTTServer:
    """Minimal MQTT broker for BambuLab slicer communication."""

    def __init__(
        self,
        serial: str,
        access_code: str,
        cert_path: Path,
        key_path: Path,
        port: int = MQTT_PORT,
        on_print_command: Callable | None = None,
        on_mqtt_command: Callable | None = None,
    ):
        self.serial = serial
        self.access_code = access_code
        self.cert_path = cert_path
        self.key_path = key_path
        self.port = port
        self.on_print_command = on_print_command
        self.on_mqtt_command = on_mqtt_command
        self._running = False
        self._server = None
        self._clients: dict[str, asyncio.StreamWriter] = {}
        self._status_push_task: asyncio.Task | None = None
        self._sequence_id = 0

        # Dynamic state for status reports (updated externally by middleware)
        self._status_data: dict | None = None

        # Fallback static state
        self._gcode_state = "IDLE"
        self._current_file = ""
        self._prepare_percent = "0"

    def set_status_provider(self, provider: Callable[[], dict]) -> None:
        """Set a callable that provides current status data.

        This is called by the middleware to provide real-time printer status
        for MQTT status pushes.
        """
        self._status_provider = provider

    async def start(self) -> None:
        if self._running:
            return

        logger.info("Starting MQTT server on port %s", self.port)

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(str(self.cert_path), str(self.key_path))
        ssl_context.verify_mode = ssl.CERT_NONE
        ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
        ssl_context.check_hostname = False

        try:
            self._running = True

            async def connection_handler(reader, writer):
                try:
                    addr = writer.get_extra_info("peername")
                    ssl_obj = writer.get_extra_info("ssl_object")
                    if ssl_obj:
                        logger.info("MQTT TLS connection from %s", addr)
                    await self._handle_client(reader, writer)
                except ssl.SSLError as e:
                    logger.error("MQTT SSL error: %s", e)
                except Exception as e:
                    logger.error("MQTT connection handler error: %s", e)

            self._server = await asyncio.start_server(
                connection_handler,
                "0.0.0.0",
                self.port,
                ssl=ssl_context,
            )

            logger.info("MQTT server listening on port %s", self.port)

            self._status_push_task = asyncio.create_task(self._periodic_status_push())

            async with self._server:
                await self._server.serve_forever()

        except OSError as e:
            if e.errno == 98:
                logger.error("MQTT port %s is already in use", self.port)
            else:
                logger.error("MQTT server error: %s", e)
        except asyncio.CancelledError:
            logger.debug("MQTT server task cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        logger.info("Stopping MQTT server")
        self._running = False

        if self._status_push_task:
            self._status_push_task.cancel()
            try:
                await self._status_push_task
            except asyncio.CancelledError:
                pass
            self._status_push_task = None

        for _client_id, writer in list(self._clients.items()):
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass
        self._clients.clear()

        if self._server:
            try:
                self._server.close()
                await self._server.wait_closed()
            except OSError:
                pass
            self._server = None

    async def _periodic_status_push(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(1)

                disconnected = []
                for client_id, writer in list(self._clients.items()):
                    try:
                        if writer.is_closing():
                            disconnected.append(client_id)
                            continue
                        await self._send_status_report(writer)
                    except OSError as e:
                        logger.debug("Failed to push status to %s: %s", client_id, e)
                        disconnected.append(client_id)

                for client_id in disconnected:
                    self._clients.pop(client_id, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Periodic status push error: %s", e)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        addr = writer.get_extra_info("peername")
        client_id = f"{addr[0]}:{addr[1]}" if addr else "unknown"
        logger.info("MQTT client connected: %s", client_id)

        authenticated = False

        try:
            while self._running:
                try:
                    header = await asyncio.wait_for(reader.read(1), timeout=60)
                except TimeoutError:
                    break

                if not header:
                    break

                packet_type = (header[0] & 0xF0) >> 4

                remaining_length = await self._read_remaining_length(reader)
                if remaining_length is None:
                    break

                payload = await reader.read(remaining_length) if remaining_length > 0 else b""

                if packet_type == 1:  # CONNECT
                    authenticated = await self._handle_connect(payload, writer)
                    if not authenticated:
                        break
                    self._clients[client_id] = writer
                elif packet_type == 3:  # PUBLISH
                    if authenticated:
                        await self._handle_publish(header[0], payload, writer)
                elif packet_type == 8:  # SUBSCRIBE
                    if authenticated:
                        await self._handle_subscribe(payload, writer)
                elif packet_type == 12:  # PINGREQ
                    writer.write(bytes([0xD0, 0x00]))
                    await writer.drain()
                elif packet_type == 14:  # DISCONNECT
                    break

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("MQTT client error: %s", e)
        finally:
            self._clients.pop(client_id, None)
            try:
                writer.close()
                await writer.wait_closed()
            except OSError:
                pass

    async def _read_remaining_length(self, reader: asyncio.StreamReader) -> int | None:
        multiplier = 1
        value = 0

        for _ in range(4):
            try:
                byte = await reader.read(1)
                if not byte:
                    return None
                encoded = byte[0]
                value += (encoded & 127) * multiplier
                if (encoded & 128) == 0:
                    return value
                multiplier *= 128
            except OSError:
                return None

        return None

    async def _handle_connect(self, payload: bytes, writer: asyncio.StreamWriter) -> bool:
        try:
            idx = 0
            proto_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2 + proto_len
            idx += 2  # protocol level + connect flags
            idx += 2  # keepalive

            client_id_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            idx += client_id_len

            username_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            username = payload[idx : idx + username_len].decode("utf-8")
            idx += username_len

            password_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            password = payload[idx : idx + password_len].decode("utf-8")

            if username == "bblp" and password == self.access_code:
                writer.write(bytes([0x20, 0x02, 0x00, 0x00]))
                await writer.drain()
                logger.info("MQTT client authenticated successfully")
                await self._send_status_report(writer)
                return True
            else:
                writer.write(bytes([0x20, 0x02, 0x00, 0x05]))
                await writer.drain()
                logger.warning("MQTT auth failed for user '%s'", username)
                return False

        except (IndexError, ValueError) as e:
            logger.debug("MQTT CONNECT parse error: %s", e)
            writer.write(bytes([0x20, 0x02, 0x00, 0x02]))
            await writer.drain()
            return False

    async def _handle_subscribe(self, payload: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            packet_id = (payload[0] << 8) | payload[1]

            idx = 2
            granted_qos = []
            while idx < len(payload):
                topic_len = (payload[idx] << 8) | payload[idx + 1]
                idx += 2
                topic = payload[idx : idx + topic_len].decode("utf-8")
                idx += topic_len
                requested_qos = payload[idx]
                idx += 1

                logger.info("MQTT subscribe: %s QoS=%s", topic, requested_qos)
                granted_qos.append(min(requested_qos, 1))

            suback = bytes([0x90, 2 + len(granted_qos), packet_id >> 8, packet_id & 0xFF])
            suback += bytes(granted_qos)
            writer.write(suback)
            await writer.drain()

            await self._send_status_report(writer)

        except (IndexError, ValueError, OSError) as e:
            logger.debug("MQTT SUBSCRIBE error: %s", e)

    async def _send_status_report(self, writer: asyncio.StreamWriter) -> None:
        try:
            self._sequence_id += 1

            # Use status from middleware if available
            if hasattr(self, "_status_provider") and self._status_provider:
                status = self._status_provider()
                # Inject sequence_id
                if "print" in status:
                    status["print"]["sequence_id"] = str(self._sequence_id)
            else:
                # Fallback static status
                status = self._build_default_status()

            await self._publish_to_report(writer, status)

        except OSError as e:
            logger.error("Failed to send status report: %s", e)

    def _build_default_status(self) -> dict:
        """Build a default BambuLab status message."""
        return {
            "print": {
                "sequence_id": str(self._sequence_id),
                "command": "push_status",
                "msg": 0,
                "gcode_state": self._gcode_state,
                "gcode_file": self._current_file,
                "gcode_file_prepare_percent": self._prepare_percent,
                "subtask_name": self._current_file.replace(".3mf", "") if self._current_file else "",
                "mc_print_stage": "",
                "mc_percent": 0,
                "mc_remaining_time": 0,
                "wifi_signal": "-44dBm",
                "print_error": 0,
                "print_type": "",
                "bed_temper": 25.0,
                "bed_target_temper": 0.0,
                "nozzle_temper": 25.0,
                "nozzle_target_temper": 0.0,
                "chamber_temper": 25.0,
                "cooling_fan_speed": "0",
                "big_fan1_speed": "0",
                "big_fan2_speed": "0",
                "heatbreak_fan_speed": "0",
                "spd_lvl": 1,
                "spd_mag": 100,
                "stg": [],
                "stg_cur": 0,
                "layer_num": 0,
                "total_layer_num": 0,
                "home_flag": 256,
                "hw_switch_state": 0,
                "online": {"ahb": False, "rfid": False, "version": 7},
                "ams_status": 0,
                "sdcard": True,
                "storage": {"free": 1000000000, "total": 32000000000},
                "upgrade_state": {
                    "sequence_id": 0,
                    "progress": "",
                    "status": "",
                    "consistency_request": False,
                    "dis_state": 0,
                    "err_code": 0,
                    "force_upgrade": False,
                    "message": "",
                    "module": "",
                    "new_version_state": 2,
                    "new_ver_list": [],
                    "ota_new_version_number": "",
                    "ahb_new_version_number": "",
                },
                "ipcam": {
                    "ipcam_dev": "1",
                    "ipcam_record": "enable",
                    "timelapse": "disable",
                    "resolution": "1080p",
                    "mode_bits": 0,
                },
                "xcam": {
                    "allow_skip_parts": False,
                    "buildplate_marker_detector": True,
                    "first_layer_inspector": True,
                    "halt_print_sensitivity": "medium",
                    "print_halt": True,
                    "printing_monitor": True,
                    "spaghetti_detector": True,
                },
                "lights_report": [{"node": "chamber_light", "mode": "on"}],
                "nozzle_diameter": "0.4",
                "nozzle_type": "hardened_steel",
            }
        }

    async def _send_version_response(self, writer: asyncio.StreamWriter, sequence_id: str) -> None:
        try:
            version_info = {
                "info": {
                    "command": "get_version",
                    "sequence_id": sequence_id,
                    "module": [
                        {
                            "name": "ota",
                            "product_name": "X1 Carbon",
                            "sw_ver": "01.07.00.00",
                            "sw_new_ver": "",
                            "hw_ver": "OTA",
                            "sn": self.serial,
                            "flag": 0,
                        },
                        {
                            "name": "esp32",
                            "product_name": "X1 Carbon",
                            "sw_ver": "01.07.22.25",
                            "sw_new_ver": "",
                            "hw_ver": "AP05",
                            "sn": self.serial,
                            "flag": 0,
                        },
                    ],
                }
            }

            await self._publish_to_report(writer, version_info)
            logger.info("Sent version response")

        except OSError as e:
            logger.error("Failed to send version response: %s", e)

    def set_gcode_state(self, state: str, filename: str = "", prepare_percent: str = "0") -> None:
        self._gcode_state = state
        self._current_file = filename
        self._prepare_percent = prepare_percent

    async def _publish_to_report(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        topic = f"device/{self.serial}/report"
        message = json.dumps(payload)

        topic_bytes = topic.encode("utf-8")
        message_bytes = message.encode("utf-8")

        remaining = 2 + len(topic_bytes) + len(message_bytes)
        packet = bytes([0x30])  # PUBLISH, QoS 0

        while remaining > 0:
            byte = remaining % 128
            remaining //= 128
            if remaining > 0:
                byte |= 0x80
            packet += bytes([byte])

        packet += bytes([len(topic_bytes) >> 8, len(topic_bytes) & 0xFF])
        packet += topic_bytes
        packet += message_bytes

        writer.write(packet)
        try:
            await asyncio.wait_for(writer.drain(), timeout=5)
        except TimeoutError:
            logger.debug("MQTT drain timeout — client may be busy")

    async def _send_print_response(self, writer: asyncio.StreamWriter, sequence_id: str, filename: str) -> None:
        self._gcode_state = "PREPARE"
        self._current_file = filename
        self._prepare_percent = "0"

        try:
            subtask_name = filename.replace(".3mf", "") if filename else ""
            response = {
                "print": {
                    "command": "project_file",
                    "sequence_id": sequence_id,
                    "param": "Metadata/plate_1.gcode",
                    "subtask_name": subtask_name,
                    "gcode_state": "PREPARE",
                    "gcode_file": filename,
                    "gcode_file_prepare_percent": "0",
                    "result": "SUCCESS",
                    "msg": 0,
                }
            }
            await self._publish_to_report(writer, response)
            logger.info("Sent project_file acknowledgment for %s", filename)
        except OSError as e:
            logger.error("Failed to send print response: %s", e)

    async def _handle_publish(self, header: int, payload: bytes, writer: asyncio.StreamWriter) -> None:
        try:
            idx = 0
            topic_len = (payload[idx] << 8) | payload[idx + 1]
            idx += 2
            topic = payload[idx : idx + topic_len].decode("utf-8")
            idx += topic_len

            qos = (header & 0x06) >> 1
            if qos > 0:
                idx += 2

            message = payload[idx:].decode("utf-8")

            logger.info("MQTT publish to %s: %s...", topic, message[:100])

            if f"device/{self.serial}/request" in topic:
                try:
                    data = json.loads(message)

                    if "pushing" in data:
                        pushing_data = data["pushing"]
                        command = pushing_data.get("command", "")
                        if command in ("pushall", "start"):
                            await self._send_status_report(writer)

                    if "info" in data:
                        info_data = data["info"]
                        command = info_data.get("command", "")
                        sequence_id = info_data.get("sequence_id", "0")
                        if command == "get_version":
                            await self._send_version_response(writer, sequence_id)

                    if "print" in data:
                        print_data = data["print"]
                        command = print_data.get("command", "")
                        filename = print_data.get("subtask_name", "")
                        sequence_id = print_data.get("sequence_id", "0")

                        if command == "project_file":
                            file_3mf = print_data.get("file", filename)
                            await self._send_print_response(writer, sequence_id, file_3mf)

                            if self.on_print_command:
                                await self._notify_print_command(filename, print_data)

                    # Forward full MQTT command to middleware for translation
                    if self.on_mqtt_command:
                        try:
                            data_parsed = json.loads(message)
                            result = self.on_mqtt_command(data_parsed)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            logger.error("MQTT command callback error: %s", e)

                except json.JSONDecodeError:
                    pass

        except (IndexError, ValueError, OSError) as e:
            logger.debug("MQTT PUBLISH error: %s", e)

    async def _notify_print_command(self, filename: str, data: dict) -> None:
        if self.on_print_command:
            try:
                result = self.on_print_command(filename, data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error("Print command callback error: %s", e)
