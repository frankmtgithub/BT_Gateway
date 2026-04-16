"""Configuration management for BT Gateway.

Handles loading, saving, and thread-safe access to the JSON config file.
"""

import json
import os
import threading
import logging

logger = logging.getLogger(__name__)

# Range of RFCOMM "COM port" numbers we will hand out to paired devices.
# Matches the kernel's RFCOMM TTY range (/dev/rfcomm0 .. /dev/rfcomm30).
PORT_MIN = 0
PORT_MAX = 30

# Range of RFCOMM channels we advertise SPP on for paired devices.
# RFCOMM channels are 1..30 on classic Bluetooth.
LISTEN_CHANNEL_MIN = 1
LISTEN_CHANNEL_MAX = 30
DEFAULT_LISTEN_CHANNEL = 1

DEFAULT_CONFIG = {
    "plc_adapter": "",
    "device_adapter": "",
    # 0 means "auto-discover via SDP on every connect attempt".  Non-zero
    # forces that channel number and skips the SDP lookup — used as an
    # escape hatch if SDP browsing doesn't work against the PLC.
    "plc_channel": 0,
    # Purely informational label for the Windows-side COM port the user
    # opened on the PLC (e.g. "COM6").  Not used by the connection logic —
    # the actual RFCOMM channel comes from SDP discovery.
    "plc_com_port": "",
    "plc_port": 0,
    "plc_reconnect_interval": 5,
    # Keep-alive message periodically sent to the PLC while connected.
    # ``plc_keepalive_interval`` is in seconds; 0 disables the keep-alive.
    # ``plc_keepalive_message`` is the literal payload (a newline is appended
    # by the sender, same as normal PLC messages).  Empty message also
    # disables it.
    "plc_keepalive_enabled": False,
    "plc_keepalive_interval": 30,
    "plc_keepalive_message": "PING",
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "debug_mode": False,
    "devices": {}
}


class Config:
    """Thread-safe configuration manager backed by a JSON file.

    The devices dict maps BT addresses to device info:
        {
            "AA:BB:CC:DD:EE:FF": {
                "name": "connection1",
                "paired": true,
                "port": 1,
                "enabled": true,
                "listen_channel": 1
            }
        }

    ``enabled`` controls whether the gateway accepts SPP connections from
    this device — when false, the matching listening channel is closed and
    inbound RFCOMM connections from this address are rejected.

    ``listen_channel`` is the RFCOMM channel on the device adapter that
    this device should hit when it initiates its SPP connection (the
    "COM port" the scanner or remote Pi expects to find on the gateway).
    """

    def __init__(self, config_path="/data/config.json"):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._data = dict(DEFAULT_CONFIG)
        self._data["devices"] = {}
        self.load()

    def load(self):
        with self._lock:
            if os.path.exists(self.config_path):
                try:
                    with open(self.config_path, "r") as f:
                        loaded = json.load(f)
                    for key in DEFAULT_CONFIG:
                        if key in loaded:
                            self._data[key] = loaded[key]
                    # Migration: ensure every device has a port.  Older
                    # configs may not have one.
                    migrated = False
                    used = set()
                    for addr, dev in list(self._data.get("devices", {}).items()):
                        if "port" in dev and dev["port"] is not None:
                            used.add(dev["port"])
                    plc_port = self._data.get("plc_port")
                    if plc_port is not None:
                        used.add(plc_port)
                    next_free = PORT_MIN
                    for addr, dev in self._data.get("devices", {}).items():
                        if "port" not in dev or dev["port"] is None:
                            while next_free in used and next_free <= PORT_MAX:
                                next_free += 1
                            if next_free <= PORT_MAX:
                                dev["port"] = next_free
                                used.add(next_free)
                                migrated = True
                                next_free += 1
                        # Backfill the newer enabled / listen_channel fields.
                        if "enabled" not in dev:
                            dev["enabled"] = True
                            migrated = True
                        if "listen_channel" not in dev or \
                                not self._is_valid_listen_channel(dev.get("listen_channel")):
                            dev["listen_channel"] = DEFAULT_LISTEN_CHANNEL
                            migrated = True
                    if migrated:
                        self._save_unlocked()
                    logger.info("Configuration loaded from %s", self.config_path)
                except (json.JSONDecodeError, OSError) as e:
                    logger.error("Failed to load config: %s", e)
            else:
                logger.info("No config file found, using defaults")
                self._save_unlocked()

    def save(self):
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self):
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        try:
            with open(self.config_path, "w") as f:
                json.dump(self._data, f, indent=2)
            logger.info("Configuration saved to %s", self.config_path)
        except OSError as e:
            logger.error("Failed to save config: %s", e)

    def get(self, key, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key, value):
        with self._lock:
            self._data[key] = value
            self._save_unlocked()

    @property
    def data(self):
        with self._lock:
            return json.loads(json.dumps(self._data))

    # ── Devices ─────────────────────────────────────────────────────────

    def add_device(self, address, name=None):
        """Add a paired device. Auto-assigns a name and a port if none provided.

        Returns the device entry dict (name + port).
        """
        with self._lock:
            if address in self._data["devices"]:
                return dict(self._data["devices"][address])
            if name is None:
                existing_nums = []
                for dev in self._data["devices"].values():
                    dev_name = dev.get("name", "")
                    if dev_name.startswith("connection"):
                        try:
                            existing_nums.append(int(dev_name[10:]))
                        except ValueError:
                            pass
                next_num = max(existing_nums, default=0) + 1
                name = f"connection{next_num}"
            port = self._next_unused_port_unlocked()
            self._data["devices"][address] = {
                "name": name,
                "paired": True,
                "port": port,
                "enabled": True,
                "listen_channel": DEFAULT_LISTEN_CHANNEL,
            }
            self._save_unlocked()
            logger.info("Device added: %s as %s on port %s", address, name, port)
            return dict(self._data["devices"][address])

    def remove_device(self, address):
        with self._lock:
            if address in self._data["devices"]:
                freed = self._data["devices"][address].get("port")
                del self._data["devices"][address]
                self._save_unlocked()
                logger.info("Device removed: %s (port %s released)",
                            address, freed)
                return True
            return False

    def remove_all_devices(self):
        with self._lock:
            self._data["devices"] = {}
            self._save_unlocked()
            logger.info("All devices removed")

    def rename_device(self, address, new_name):
        with self._lock:
            if address in self._data["devices"]:
                self._data["devices"][address]["name"] = new_name
                self._save_unlocked()
                logger.info("Device %s renamed to %s", address, new_name)
                return True
            return False

    def set_device_port(self, address, port):
        """Re-assign a device's port.  Port must be in range and unused."""
        with self._lock:
            if address not in self._data["devices"]:
                return False
            if not (PORT_MIN <= port <= PORT_MAX):
                return False
            # Is the port in use by somebody else?
            for addr, dev in self._data["devices"].items():
                if addr != address and dev.get("port") == port:
                    return False
            if self._data.get("plc_port") == port:
                return False
            self._data["devices"][address]["port"] = port
            self._save_unlocked()
            logger.info("Device %s port set to %s", address, port)
            return True

    def get_device_name(self, address):
        with self._lock:
            dev = self._data["devices"].get(address)
            return dev["name"] if dev else None

    def get_device_address(self, name):
        """Look up a device address by its connection name."""
        with self._lock:
            for addr, dev in self._data["devices"].items():
                if dev.get("name") == name:
                    return addr
            return None

    def get_devices(self):
        with self._lock:
            return dict(self._data.get("devices", {}))

    def get_device_port(self, address):
        with self._lock:
            dev = self._data["devices"].get(address)
            return dev.get("port") if dev else None

    # ── Enabled / listen-channel (per-device SPP listener config) ──────

    def set_device_enabled(self, address, enabled):
        """Enable or disable the SPP listener for a specific device."""
        with self._lock:
            if address not in self._data["devices"]:
                return False
            self._data["devices"][address]["enabled"] = bool(enabled)
            self._save_unlocked()
            logger.info("Device %s enabled=%s", address, bool(enabled))
            return True

    def set_device_listen_channel(self, address, channel):
        """Change the RFCOMM channel we advertise SPP on for this device.

        Returns True on success, False if the device is unknown, the
        channel is out of range (1-30), or the channel is already
        claimed by another enabled paired device.  Channel ownership is
        exclusive so that a scanner can only land on its own listener.
        """
        try:
            channel = int(channel)
        except (TypeError, ValueError):
            return False
        if not self._is_valid_listen_channel(channel):
            return False
        with self._lock:
            if address not in self._data["devices"]:
                return False
            # Reject duplicates: another enabled device already listens on
            # this channel.  Disabled devices don't block the slot because
            # their listener isn't active.
            for other_addr, other_dev in self._data["devices"].items():
                if other_addr == address:
                    continue
                if not other_dev.get("enabled", True):
                    continue
                if int(other_dev.get("listen_channel") or 0) == channel:
                    logger.warning(
                        "Refused listen_channel=%d for %s — already "
                        "owned by %s",
                        channel, address, other_addr,
                    )
                    return False
            self._data["devices"][address]["listen_channel"] = channel
            self._save_unlocked()
            logger.info("Device %s listen_channel=%d", address, channel)
            return True

    def get_enabled_devices(self):
        """Return a dict of only the devices marked enabled."""
        with self._lock:
            return {
                addr: dict(dev)
                for addr, dev in self._data.get("devices", {}).items()
                if dev.get("enabled", True)
            }

    @staticmethod
    def _is_valid_listen_channel(channel):
        try:
            n = int(channel)
        except (TypeError, ValueError):
            return False
        return LISTEN_CHANNEL_MIN <= n <= LISTEN_CHANNEL_MAX

    # ── Port management ────────────────────────────────────────────────

    def _used_ports_unlocked(self, exclude_address=None):
        used = set()
        for addr, dev in self._data["devices"].items():
            if addr == exclude_address:
                continue
            p = dev.get("port")
            if p is not None:
                used.add(p)
        plc_port = self._data.get("plc_port")
        if plc_port is not None:
            used.add(plc_port)
        return used

    def _next_unused_port_unlocked(self):
        used = self._used_ports_unlocked()
        for p in range(PORT_MIN, PORT_MAX + 1):
            if p not in used:
                return p
        return None

    def available_ports(self, exclude_address=None):
        """Return sorted list of currently unused ports."""
        with self._lock:
            used = self._used_ports_unlocked(exclude_address)
            return [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in used]

    def set_plc_port(self, port):
        with self._lock:
            if not (PORT_MIN <= port <= PORT_MAX):
                return False
            # Make sure no device holds this port
            for addr, dev in self._data["devices"].items():
                if dev.get("port") == port:
                    return False
            self._data["plc_port"] = port
            self._save_unlocked()
            logger.info("PLC port set to %s", port)
            return True
