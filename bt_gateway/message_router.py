"""Message router for BT Gateway.

Routes messages between the PLC connection and device connections.

Protocol:
    - Device → PLC: raw data from device is wrapped in JSON:
        {"device_id": "<name>", "message": "<raw data>"}
    - PLC → Device: JSON from PLC is parsed and routed:
        {"device_id": "<name>", "message": "<raw data>"}
      The "message" field is sent as raw data to the target device.
"""

import json
import logging
import threading

logger = logging.getLogger(__name__)


class MessageRouter:
    """Routes messages between PLC and device connections."""

    def __init__(self, config, socketio=None):
        self._config = config
        self._socketio = socketio
        self._plc_connection = None
        self._device_connections = {}  # address -> DeviceConnection
        self._lock = threading.Lock()

    def set_plc_connection(self, plc_conn):
        self._plc_connection = plc_conn

    def register_device(self, address, connection):
        with self._lock:
            self._device_connections[address] = connection
            logger.info("Device registered: %s", address)
            self._emit_status()

    def unregister_device(self, address):
        with self._lock:
            if address in self._device_connections:
                del self._device_connections[address]
                logger.info("Device unregistered: %s", address)
                self._emit_status()

    def get_device_connection(self, address):
        with self._lock:
            return self._device_connections.get(address)

    def get_connected_devices(self):
        with self._lock:
            return list(self._device_connections.keys())

    def route_from_device(self, device_address, raw_data):
        """Route a message from a device to the PLC.

        Wraps raw data in JSON with the device's connection name.
        """
        device_name = self._config.get_device_name(device_address)
        if device_name is None:
            logger.warning(
                "Received data from unknown device %s, dropping", device_address
            )
            return False

        message = json.dumps({
            "device_id": device_name,
            "message": raw_data
        })

        if self._plc_connection and self._plc_connection.is_connected:
            try:
                self._plc_connection.send(message)
                logger.debug("Routed message from %s to PLC: %s", device_name, message[:100])
                self._emit_message_log("device_to_plc", device_name, raw_data[:200])
                return True
            except Exception as e:
                logger.error("Failed to send to PLC: %s", e)
                return False
        else:
            logger.warning("PLC not connected, dropping message from %s", device_name)
            return False

    def route_from_plc(self, json_data):
        """Route a message from the PLC to the appropriate device.

        Parses JSON to extract device_id and sends the raw message to that device.
        """
        try:
            parsed = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON from PLC: %s — %s", e, json_data[:200])
            return False

        device_id = parsed.get("device_id")
        message = parsed.get("message")

        if not device_id or message is None:
            logger.error("PLC message missing device_id or message: %s", json_data[:200])
            return False

        # Look up the device address by its connection name
        device_address = self._config.get_device_address(device_id)
        if not device_address:
            logger.warning("No device found with name '%s'", device_id)
            return False

        with self._lock:
            conn = self._device_connections.get(device_address)

        if conn is None:
            logger.warning("Device '%s' (%s) not connected", device_id, device_address)
            return False

        try:
            conn.send(message)
            logger.debug("Routed message from PLC to %s: %s", device_id, message[:100])
            self._emit_message_log("plc_to_device", device_id, message[:200])
            return True
        except Exception as e:
            logger.error("Failed to send to device %s: %s", device_id, e)
            return False

    def _emit_status(self):
        if self._socketio:
            self._socketio.emit("status_update", self.get_status(), namespace="/")

    def _emit_message_log(self, direction, device_name, preview):
        if self._socketio:
            self._socketio.emit("message_log", {
                "direction": direction,
                "device": device_name,
                "preview": preview,
            }, namespace="/")

    def get_status(self):
        """Build a full status dict for the web UI."""
        plc_status = "disconnected"
        if self._plc_connection:
            plc_status = self._plc_connection.status

        devices_status = {}
        config_devices = self._config.get_devices()
        with self._lock:
            connected_addrs = set(self._device_connections.keys())

        for addr, info in config_devices.items():
            devices_status[addr] = {
                "name": info["name"],
                "connected": addr in connected_addrs,
                "address": addr,
            }

        return {
            "plc": {
                "status": plc_status,
                "address": self._config.get("plc_address", ""),
                "adapter": self._config.get("plc_adapter", ""),
            },
            "devices": devices_status,
            "device_adapter": self._config.get("device_adapter", ""),
        }
