"""Message router for BT Gateway.

Routes messages between the PLC connection and device connections.

Protocol:
    - Device → PLC: raw data from device is wrapped in JSON:
        {"device_id": "<name>", "message": "<raw data>"}
    - PLC → Device: JSON from PLC is parsed and routed:
        {"device_id": "<name>", "message": "<raw data>"}
      The "message" field is sent as raw data to the target device.

The router also emits Socket.IO events so the web UI can display every
received message in real time.  Two channels are emitted:

    * ``message_log``  — one event per fully-formed, routed message.  Always
      emitted, shown in the normal Logs panel.  Carries the full message
      payload (not a preview).
    * ``debug_log``    — raw receive chunks, per connection.  Only emitted
      when debug mode is enabled in the config.  Useful for diagnosing
      scanners / PLCs that send odd line terminators or binary bursts.
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

    # ── Debug / raw notifications ──────────────────────────────────────

    def notify_device_raw(self, device_address, raw_chunk):
        """Report a raw bytes chunk received from a device (debug mode)."""
        if not self._config.get("debug_mode", False):
            return
        name = self._config.get_device_name(device_address) or device_address
        self._emit_debug_log("device", name, device_address, raw_chunk)

    def notify_plc_raw(self, raw_chunk):
        """Report a raw bytes chunk received from the PLC (debug mode)."""
        if not self._config.get("debug_mode", False):
            return
        plc_addr = ""
        if self._plc_connection:
            plc_addr = getattr(self._plc_connection, "address", "") or ""
        self._emit_debug_log("plc", "PLC", plc_addr, raw_chunk)

    # ── Routing ────────────────────────────────────────────────────────

    def route_from_device(self, device_address, raw_data):
        """Route a message from a device to the PLC.

        Wraps raw data in JSON with the device's connection name.
        """
        device_name = self._config.get_device_name(device_address)
        if device_name is None:
            logger.warning(
                "Received data from unknown device %s, dropping", device_address
            )
            # Still log it so the UI can show unexpected traffic.
            self._emit_message_log(
                "device_to_plc",
                f"unknown({device_address})",
                device_address,
                raw_data,
                delivered=False,
            )
            return False

        # Always show every received message in the log, even when the PLC
        # is not currently connected.
        delivered = False
        message = json.dumps({
            "device_id": device_name,
            "message": raw_data
        })

        if self._plc_connection and self._plc_connection.is_connected:
            try:
                self._plc_connection.send(message)
                logger.debug("Routed message from %s to PLC: %s",
                             device_name, message)
                delivered = True
            except Exception as e:
                logger.error("Failed to send to PLC: %s", e)
        else:
            logger.warning("PLC not connected, message from %s not forwarded",
                           device_name)

        self._emit_message_log(
            "device_to_plc", device_name, device_address, raw_data,
            delivered=delivered,
        )
        return delivered

    def route_from_plc(self, json_data):
        """Route a message from the PLC to the appropriate device.

        Parses JSON to extract device_id and sends the raw message to that device.
        """
        try:
            parsed = json.loads(json_data)
        except json.JSONDecodeError as e:
            logger.error("Invalid JSON from PLC: %s — %s", e, json_data)
            self._emit_message_log(
                "plc_to_device", "PLC", "", json_data,
                delivered=False, error="invalid_json",
            )
            return False

        device_id = parsed.get("device_id")
        message = parsed.get("message")

        if not device_id or message is None:
            logger.error("PLC message missing device_id or message: %s", json_data)
            self._emit_message_log(
                "plc_to_device", "PLC", "", json_data,
                delivered=False, error="missing_fields",
            )
            return False

        # Look up the device address by its connection name
        device_address = self._config.get_device_address(device_id)
        if not device_address:
            logger.warning("No device found with name '%s'", device_id)
            self._emit_message_log(
                "plc_to_device", device_id, "", message,
                delivered=False, error="unknown_device",
            )
            return False

        with self._lock:
            conn = self._device_connections.get(device_address)

        if conn is None:
            logger.warning("Device '%s' (%s) not connected",
                           device_id, device_address)
            self._emit_message_log(
                "plc_to_device", device_id, device_address, message,
                delivered=False, error="not_connected",
            )
            return False

        delivered = False
        try:
            conn.send(message)
            logger.debug("Routed message from PLC to %s: %s", device_id, message)
            delivered = True
        except Exception as e:
            logger.error("Failed to send to device %s: %s", device_id, e)

        self._emit_message_log(
            "plc_to_device", device_id, device_address, message,
            delivered=delivered,
        )
        return delivered

    # ── Socket.IO emitters ─────────────────────────────────────────────

    def _emit_status(self):
        if self._socketio:
            self._socketio.emit("status_update", self.get_status(), namespace="/")

    def _emit_message_log(self, direction, device_name, device_address,
                          message, delivered=True, error=None):
        if not self._socketio:
            return
        self._socketio.emit("message_log", {
            "direction": direction,
            "device": device_name,
            "address": device_address,
            "message": message,
            "delivered": delivered,
            "error": error,
        }, namespace="/")

    def _emit_debug_log(self, source, name, address, raw_chunk):
        if not self._socketio:
            return
        self._socketio.emit("debug_log", {
            "source": source,        # "device" or "plc"
            "name": name,            # connection name or "PLC"
            "address": address,      # BT MAC address of the source
            "raw": raw_chunk,
        }, namespace="/")

    # ── Status snapshot ────────────────────────────────────────────────

    def get_status(self):
        """Build a full status dict for the web UI."""
        plc_status = "disconnected"
        plc_address = ""
        plc_effective_channel = 0
        if self._plc_connection:
            plc_status = self._plc_connection.status
            plc_address = getattr(self._plc_connection, "address", "") or ""
            plc_effective_channel = int(
                getattr(self._plc_connection, "channel", 0) or 0
            )

        devices_status = {}
        config_devices = self._config.get_devices()
        with self._lock:
            connected_addrs = set(self._device_connections.keys())

        for addr, info in config_devices.items():
            devices_status[addr] = {
                "name": info["name"],
                "connected": addr in connected_addrs,
                "address": addr,
                "port": info.get("port"),
                "enabled": bool(info.get("enabled", True)),
                "listen_channel": int(info.get("listen_channel", 1) or 1),
            }

        return {
            "plc": {
                "status": plc_status,
                "address": plc_address,
                "adapter": self._config.get("plc_adapter", ""),
                # Configured override (0 = auto-discover via SDP).
                "channel": int(self._config.get("plc_channel", 0) or 0),
                # Actual channel used for the current/last connection —
                # this is the one discovered from the PLC's SDP record.
                "effective_channel": plc_effective_channel,
                # Windows-side COM port the user opened (informational,
                # doesn't affect the connection, just helps the user tie
                # the Pi-side status back to "my Hercules on COM6").
                "com_port": self._config.get("plc_com_port", ""),
                "port": self._config.get("plc_port", 0),
            },
            "devices": devices_status,
            "device_adapter": self._config.get("device_adapter", ""),
            "debug_mode": bool(self._config.get("debug_mode", False)),
        }
