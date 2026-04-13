"""Bluetooth adapter management via BlueZ D-Bus API.

Provides adapter enumeration, property control, discovery, and pairing
through the BlueZ D-Bus interfaces (Adapter1, Device1, etc.).
"""

import logging
import re
import shutil
import subprocess
import threading

import dbus
import dbus.mainloop.glib
from gi.repository import GLib

SPP_UUID_SHORT = "1101"
SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

logger = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"


class BluetoothManager:
    """Manages BlueZ adapters, discovery, and pairing via D-Bus."""

    def __init__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SystemBus()
        self._loop = GLib.MainLoop()
        self._loop_thread = None
        self._discovering = False
        self._discovery_adapter_path = None

    def start(self):
        """Start the GLib main loop in a background thread for D-Bus signals."""
        if self._loop_thread is None or not self._loop_thread.is_alive():
            self._loop_thread = threading.Thread(
                target=self._loop.run, daemon=True, name="glib-mainloop"
            )
            self._loop_thread.start()
            logger.info("GLib main loop started")

    def stop(self):
        if self._loop.is_running():
            self._loop.quit()
            logger.info("GLib main loop stopped")

    def list_adapters(self):
        """Return a list of available BT adapters with their properties.

        Returns:
            list of dict: [{"path": "/org/bluez/hci0", "name": "hci0",
                            "address": "AA:BB:...", "powered": True}, ...]
        """
        adapters = []
        try:
            om = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE
            )
            objects = om.GetManagedObjects()
            for path, interfaces in objects.items():
                if ADAPTER_IFACE in interfaces:
                    props = interfaces[ADAPTER_IFACE]
                    adapters.append({
                        "path": str(path),
                        "name": str(path).split("/")[-1],
                        "address": str(props.get("Address", "")),
                        "powered": bool(props.get("Powered", False)),
                        "discoverable": bool(props.get("Discoverable", False)),
                        "pairable": bool(props.get("Pairable", False)),
                        "alias": str(props.get("Alias", "")),
                    })
        except dbus.DBusException as e:
            logger.error("Failed to list adapters: %s", e)
        return adapters

    def get_adapter_address(self, adapter_name):
        """Get the BT address of a named adapter (e.g. 'hci0')."""
        for adapter in self.list_adapters():
            if adapter["name"] == adapter_name:
                return adapter["address"]
        return None

    def get_adapter_path(self, adapter_name):
        return f"/org/bluez/{adapter_name}"

    def set_adapter_property(self, adapter_name, prop, value):
        """Set a property on an adapter (e.g. Powered, Discoverable, Pairable)."""
        path = self.get_adapter_path(adapter_name)
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, path), PROPS_IFACE
            )
            if isinstance(value, bool):
                value = dbus.Boolean(value)
            props.Set(ADAPTER_IFACE, prop, value)
            logger.info("Set %s.%s = %s", adapter_name, prop, value)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to set %s on %s: %s", prop, adapter_name, e)
            return False

    def power_adapter(self, adapter_name, on=True):
        return self.set_adapter_property(adapter_name, "Powered", on)

    def set_discoverable(self, adapter_name, discoverable=True, timeout=0):
        """Make adapter discoverable. timeout=0 means indefinite."""
        self.set_adapter_property(adapter_name, "DiscoverableTimeout", dbus.UInt32(timeout))
        return self.set_adapter_property(adapter_name, "Discoverable", discoverable)

    def set_pairable(self, adapter_name, pairable=True, timeout=0):
        self.set_adapter_property(adapter_name, "PairableTimeout", dbus.UInt32(timeout))
        return self.set_adapter_property(adapter_name, "Pairable", pairable)

    def start_discovery(self, adapter_name):
        """Start device discovery on the given adapter."""
        path = self.get_adapter_path(adapter_name)
        try:
            adapter = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, path), ADAPTER_IFACE
            )
            adapter.StartDiscovery()
            self._discovering = True
            self._discovery_adapter_path = path
            logger.info("Discovery started on %s", adapter_name)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to start discovery on %s: %s", adapter_name, e)
            return False

    def stop_discovery(self, adapter_name):
        path = self.get_adapter_path(adapter_name)
        try:
            adapter = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, path), ADAPTER_IFACE
            )
            adapter.StopDiscovery()
            self._discovering = False
            self._discovery_adapter_path = None
            logger.info("Discovery stopped on %s", adapter_name)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to stop discovery on %s: %s", adapter_name, e)
            return False

    def list_devices(self, adapter_name=None):
        """List all known BT devices, optionally filtered to one adapter.

        Returns:
            list of dict with keys: path, address, name, paired, connected, trusted, adapter
        """
        devices = []
        try:
            om = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE
            )
            objects = om.GetManagedObjects()
            for path, interfaces in objects.items():
                if DEVICE_IFACE in interfaces:
                    props = interfaces[DEVICE_IFACE]
                    dev_adapter = str(props.get("Adapter", ""))
                    if adapter_name and not dev_adapter.endswith(adapter_name):
                        continue
                    devices.append({
                        "path": str(path),
                        "address": str(props.get("Address", "")),
                        "name": str(props.get("Name", props.get("Alias", "Unknown"))),
                        "paired": bool(props.get("Paired", False)),
                        "connected": bool(props.get("Connected", False)),
                        "trusted": bool(props.get("Trusted", False)),
                        "adapter": dev_adapter.split("/")[-1],
                    })
        except dbus.DBusException as e:
            logger.error("Failed to list devices: %s", e)
        return devices

    def pair_device(self, device_address, adapter_name=None):
        """Initiate pairing with a device by its BT address."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            logger.error("Device %s not found", device_address)
            return False
        try:
            device = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
            )
            device.Pair()
            # Trust the device so it can reconnect without user confirmation
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE
            )
            props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
            logger.info("Paired and trusted device %s", device_address)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to pair with %s: %s", device_address, e)
            return False

    def connect_profile(self, device_address, uuid, adapter_name=None):
        """Ask BlueZ to connect a specific profile UUID on the given device.

        This marks the connection as belonging to that profile (e.g. SPP)
        instead of whatever BlueZ decides from the device's advertised
        services (which may default to audio/HID when the remote announces
        multiple profiles).
        """
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            logger.error("Device %s not found for ConnectProfile", device_address)
            return False
        try:
            device = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
            )
            device.ConnectProfile(uuid)
            logger.info("ConnectProfile(%s) on %s", uuid, device_address)
            return True
        except dbus.DBusException as e:
            logger.warning("ConnectProfile(%s) on %s failed: %s",
                           uuid, device_address, e)
            return False

    def disconnect_profile(self, device_address, uuid, adapter_name=None):
        """Disconnect a specific profile on a device (e.g. HID) without
        touching other connected profiles."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return False
        try:
            device = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
            )
            device.DisconnectProfile(uuid)
            logger.info("DisconnectProfile(%s) on %s", uuid, device_address)
            return True
        except dbus.DBusException as e:
            logger.warning("DisconnectProfile(%s) on %s failed: %s",
                           uuid, device_address, e)
            return False

    def set_device_trusted(self, device_address, trusted=True, adapter_name=None):
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return False
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE
            )
            props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(trusted))
            return True
        except dbus.DBusException as e:
            logger.warning("Failed to set Trusted on %s: %s", device_address, e)
            return False

    def get_device_uuids(self, device_address, adapter_name=None):
        """Return the list of service UUIDs advertised by a paired device."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return []
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE
            )
            uuids = props.Get(DEVICE_IFACE, "UUIDs")
            return [str(u).lower() for u in uuids]
        except dbus.DBusException as e:
            logger.warning("Failed to read UUIDs on %s: %s", device_address, e)
            return []

    def list_paired_devices(self, adapter_name):
        """Return the paired devices on the given adapter."""
        return [d for d in self.list_devices(adapter_name) if d["paired"]]

    def get_single_paired_device(self, adapter_name):
        """Return the single paired device on an adapter, or None.

        Used by the PLC side where only one paired device is supported per
        adapter.  If multiple are present, the first is returned.
        """
        paired = self.list_paired_devices(adapter_name)
        return paired[0] if paired else None

    def remove_device(self, device_address, adapter_name=None):
        """Remove (unpair) a device."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            logger.error("Device %s not found for removal", device_address)
            return False
        # Get the adapter path from the device path
        adapter_path = "/".join(device_path.split("/")[:-1])
        try:
            adapter = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, adapter_path), ADAPTER_IFACE
            )
            adapter.RemoveDevice(device_path)
            logger.info("Removed device %s", device_address)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to remove %s: %s", device_address, e)
            return False

    def _find_device_path(self, device_address, adapter_name=None):
        """Find the D-Bus object path for a device by its BT address."""
        devices = self.list_devices(adapter_name)
        for dev in devices:
            if dev["address"].upper() == device_address.upper():
                return dev["path"]
        return None

    # ── SDP service discovery ───────────────────────────────────────────

    def sdp_find_spp_channel(self, device_address, timeout=10):
        """Browse SDP on a remote device and return the SPP RFCOMM channel.

        This is what lets the PLC connection work without the user having to
        hand-type an RFCOMM channel number: Windows (or whatever is hosting
        the PLC's SPP server) advertises the Serial Port service on some
        channel, and we read that channel out of the SDP record.

        Returns the channel number as int, or None if discovery failed /
        no Serial Port record was found.
        """
        if not shutil.which("sdptool"):
            logger.warning("sdptool not available; cannot auto-discover "
                           "SPP channel on %s", device_address)
            return None

        # `sdptool search --bdaddr <addr> SP` restricts to Serial Port.
        # Falls back to `browse` if `search` fails for some reason.
        outputs = []
        for args in (["sdptool", "search", "--bdaddr", device_address, "SP"],
                     ["sdptool", "browse", device_address]):
            try:
                result = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                )
            except (OSError, subprocess.TimeoutExpired) as e:
                logger.warning("%s failed for %s: %s", args[1], device_address, e)
                continue
            if result.returncode == 0 and result.stdout:
                outputs.append(result.stdout)
                channel = self._parse_spp_channel(result.stdout)
                if channel is not None:
                    return channel
            else:
                logger.debug("sdptool %s on %s exit=%d stderr=%s",
                             args[1], device_address, result.returncode,
                             result.stderr.strip())

        if outputs:
            logger.info("No SPP record found in SDP output for %s", device_address)
        return None

    @staticmethod
    def _parse_spp_channel(sdp_output):
        """Parse ``sdptool browse/search`` output for the SPP channel.

        sdptool emits one "Service RecHandle" block per service.  We find
        blocks whose Service Class ID List contains the Serial Port UUID
        (0x1101) and return the first RFCOMM "Channel: N" we see in such
        a block.
        """
        channel_re = re.compile(r"Channel:\s*(\d+)")
        records = re.split(r"(?m)^Service RecHandle:", sdp_output)
        for record in records:
            is_spp = (
                "0x1101" in record
                or "Serial Port" in record
                or SPP_UUID in record.lower()
            )
            if not is_spp:
                continue
            match = channel_re.search(record)
            if match:
                try:
                    return int(match.group(1))
                except ValueError:
                    continue
        return None

    @property
    def bus(self):
        return self._bus
