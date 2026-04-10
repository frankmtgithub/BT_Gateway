"""Configuration management for BT Gateway.

Handles loading, saving, and thread-safe access to the JSON config file.
"""

import json
import os
import threading
import logging

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = {
    "plc_adapter": "",
    "device_adapter": "",
    "plc_address": "",
    "plc_channel": 1,
    "plc_reconnect_interval": 5,
    "web_host": "0.0.0.0",
    "web_port": 8080,
    "devices": {}
}


class Config:
    """Thread-safe configuration manager backed by a JSON file.

    The devices dict maps BT addresses to device info:
        {
            "AA:BB:CC:DD:EE:FF": {
                "name": "connection1",
                "paired": true
            }
        }
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

    def add_device(self, address, name=None):
        """Add a paired device. Auto-assigns a name if none provided."""
        with self._lock:
            if address in self._data["devices"]:
                return self._data["devices"][address]["name"]
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
            self._data["devices"][address] = {"name": name, "paired": True}
            self._save_unlocked()
            logger.info("Device added: %s as %s", address, name)
            return name

    def remove_device(self, address):
        with self._lock:
            if address in self._data["devices"]:
                del self._data["devices"][address]
                self._save_unlocked()
                logger.info("Device removed: %s", address)
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
