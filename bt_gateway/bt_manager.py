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
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

_UUID_LABELS = {
    SPP_UUID: "SPP",
    HID_UUID: "HID",
    "0000110a-0000-1000-8000-00805f9b34fb": "A2DP-Source",
    "0000110b-0000-1000-8000-00805f9b34fb": "A2DP-Sink",
    "0000111e-0000-1000-8000-00805f9b34fb": "Handsfree",
    "00001108-0000-1000-8000-00805f9b34fb": "Headset",
}


def _uuid_label(uuid):
    """Return a short human-friendly label for a Bluetooth service UUID."""
    return _UUID_LABELS.get(str(uuid).lower(), str(uuid))

logger = logging.getLogger(__name__)

BLUEZ_SERVICE = "org.bluez"
ADAPTER_IFACE = "org.bluez.Adapter1"
DEVICE_IFACE = "org.bluez.Device1"
PROPS_IFACE = "org.freedesktop.DBus.Properties"
OM_IFACE = "org.freedesktop.DBus.ObjectManager"


class BluetoothManager:
    """Manages BlueZ adapters, discovery, and pairing via D-Bus."""

    def __init__(self, conn_log=None):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self._bus = dbus.SystemBus()
        self._loop = GLib.MainLoop()
        self._loop_thread = None
        self._discovering = False
        self._discovery_adapter_path = None
        # Optional connection log — instrumented so the UI can show every
        # low-level BlueZ step during a connection attempt.
        self._conn_log = conn_log

    def _clog(self, level, step, detail, **kw):
        if self._conn_log is None:
            return
        getattr(self._conn_log, level)(step, detail, **kw)

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
        """Toggle an adapter's Powered property.

        BlueZ refuses ``Powered=False`` while the adapter is still
        discovering or while ``Discoverable``/``Pairable`` are asserted,
        which is why the first click used to look like it did nothing
        and the second one "worked".  Clear those first, then drop
        power.  For ``on=True`` we just flip Powered.
        """
        if not on:
            # Best-effort: stop any in-flight discovery so the adapter
            # isn't holding a discovery session when we cut power.
            path = self.get_adapter_path(adapter_name)
            try:
                adapter = dbus.Interface(
                    self._bus.get_object(BLUEZ_SERVICE, path), ADAPTER_IFACE
                )
                adapter.StopDiscovery()
                self._clog("debug", "adapter.power.stop_discovery",
                           f"StopDiscovery on {adapter_name} before power-off",
                           adapter=adapter_name)
            except dbus.DBusException:
                # "No discovery started" is fine — we're just draining.
                pass
            if self._discovery_adapter_path == path:
                self._discovering = False
                self._discovery_adapter_path = None
            # Drop discoverable/pairable so bluetoothd isn't arguing with
            # the power-off request.
            self.set_adapter_property(adapter_name, "Discoverable", False)
            self.set_adapter_property(adapter_name, "Pairable", False)
            self._clog("info", "adapter.power",
                       f"Powering OFF {adapter_name}",
                       adapter=adapter_name)
        else:
            self._clog("info", "adapter.power",
                       f"Powering ON {adapter_name}",
                       adapter=adapter_name)
        return self.set_adapter_property(adapter_name, "Powered", on)

    def set_adapter_alias(self, adapter_name, alias):
        """Rename the adapter's Alias (the name other devices see)."""
        ok = self.set_adapter_property(adapter_name, "Alias", str(alias))
        if ok:
            self._clog("info", "adapter.alias",
                       f"Alias on {adapter_name} set to '{alias}'",
                       adapter=adapter_name)
        return ok

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
        """Initiate pairing with a device by its BT address.

        BlueZ's ``Device1.Pair`` is idempotent from the caller's side —
        if a previous ``Pair()`` call is still in flight, a second call
        raises ``org.bluez.Error.InProgress``.  We catch that and treat
        it as a success precondition: the user clicked twice in a row
        (or auto-connect raced the user), but the first attempt is
        still progressing.  We also try ``CancelPairing`` + retry once
        so the second click actually re-runs the flow if the first
        attempt has wedged.
        """
        self._clog("info", "pair.start",
                   f"Pairing {device_address} on {adapter_name or '(default)'}",
                   address=device_address)
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            logger.error("Device %s not found", device_address)
            self._clog("error", "pair.not_found",
                       f"Device {device_address} not present in BlueZ — "
                       "discovery still running?", address=device_address)
            return False
        device = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
        )
        props = dbus.Interface(
            self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE
        )

        # If BlueZ already says the device is paired, skip Pair() and
        # just refresh Trusted so we don't nudge a fresh authentication.
        try:
            if bool(props.Get(DEVICE_IFACE, "Paired")):
                props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
                self._clog("info", "pair.already",
                           f"{device_address} already paired; ensured Trusted",
                           address=device_address)
                return True
        except dbus.DBusException:
            pass

        for attempt in (1, 2):
            try:
                device.Pair(timeout=60000)
                props.Set(DEVICE_IFACE, "Trusted", dbus.Boolean(True))
                logger.info("Paired and trusted device %s", device_address)
                self._clog("info", "pair.ok",
                           f"Paired and trusted {device_address}",
                           address=device_address)
                return True
            except dbus.DBusException as e:
                name = getattr(e, "get_dbus_name", lambda: "")() or ""
                msg = str(e)
                # Benign: a previous Pair() call is still running. Cancel
                # it and retry once so the user's click actually takes
                # effect instead of bumping into the stale attempt.
                if "InProgress" in name or "InProgress" in msg:
                    if attempt == 1:
                        self._clog("warn", "pair.in_progress",
                                   f"Previous Pair() still running on "
                                   f"{device_address} — cancelling and retrying",
                                   address=device_address)
                        try:
                            device.CancelPairing()
                        except dbus.DBusException:
                            pass
                        continue
                logger.error("Failed to pair with %s: %s", device_address, e)
                self._clog("error", "pair.fail",
                           f"BlueZ pair failed for {device_address}: {e}",
                           address=device_address)
                return False
        return False

    def disconnect_device(self, device_address, adapter_name=None):
        """Tear down the whole ACL link to a device (all profiles)."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return False
        try:
            device = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
            )
            device.Disconnect()
            self._clog("info", "device.disconnect.ok",
                       f"Disconnected {device_address} (full ACL)",
                       address=device_address)
            return True
        except dbus.DBusException as e:
            self._clog("warn", "device.disconnect.fail",
                       f"Device1.Disconnect failed on {device_address}: {e}",
                       address=device_address)
            return False

    def is_device_connected(self, device_address, adapter_name=None):
        """Return True if BlueZ currently reports the device as connected."""
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return False
        try:
            props = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), PROPS_IFACE
            )
            return bool(props.Get(DEVICE_IFACE, "Connected"))
        except dbus.DBusException:
            return False

    def connect_profile(self, device_address, uuid, adapter_name=None,
                        silent=False):
        """Ask BlueZ to connect a specific profile UUID on the given device.

        This marks the connection as belonging to that profile (e.g. SPP)
        instead of whatever BlueZ decides from the device's advertised
        services (which may default to audio/HID when the remote announces
        multiple profiles).

        ``silent=True`` suppresses the connection-log emission (used by the
        PLC reconnect loop, which would otherwise publish two lines every
        few seconds whenever the PLC is offline).  The stdlib logger still
        sees everything.
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
            if not silent:
                self._clog("info", "profile.connect.ok",
                           f"ConnectProfile({_uuid_label(uuid)}) on {device_address}",
                           address=device_address, uuid=str(uuid))
            return True
        except dbus.DBusException as e:
            logger.warning("ConnectProfile(%s) on %s failed: %s",
                           uuid, device_address, e)
            if not silent:
                self._clog("warn", "profile.connect.fail",
                           f"ConnectProfile({_uuid_label(uuid)}) on {device_address} "
                           f"failed: {e}",
                           address=device_address, uuid=str(uuid))
            return False

    def disconnect_profile(self, device_address, uuid, adapter_name=None,
                           only_if_connected=True, only_if_advertised=True,
                           silent=False):
        """Disconnect a specific profile on a device (e.g. HID) without
        touching other connected profiles.

        By default we skip the BlueZ call entirely when either

        * the device isn't currently connected (there is nothing to
          disconnect), or
        * the profile UUID isn't in the device's advertised UUID list
          (BlueZ will reject with ``InvalidArguments`` anyway).

        Both gates avoid hammering BlueZ with calls that are guaranteed
        to fail and fill the connection log with noise — especially from
        the auto-connect loop and PLC reconnect loop, which previously
        iterated over every non-SPP UUID regardless of whether the
        remote actually spoke them.
        """
        device_path = self._find_device_path(device_address, adapter_name)
        if not device_path:
            return False

        if only_if_connected and not self.is_device_connected(
            device_address, adapter_name
        ):
            if not silent:
                self._clog("debug", "profile.disconnect.skip",
                           f"Skip DisconnectProfile({_uuid_label(uuid)}) — "
                           f"{device_address} is not currently connected",
                           address=device_address, uuid=str(uuid))
            return False

        if only_if_advertised:
            advertised = self.get_device_uuids(device_address, adapter_name)
            if str(uuid).lower() not in advertised:
                if not silent:
                    self._clog("debug", "profile.disconnect.skip",
                               f"Skip DisconnectProfile({_uuid_label(uuid)}) — "
                               f"{device_address} does not advertise this profile",
                               address=device_address, uuid=str(uuid))
                return False

        try:
            device = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, device_path), DEVICE_IFACE
            )
            device.DisconnectProfile(uuid)
            logger.info("DisconnectProfile(%s) on %s", uuid, device_address)
            if not silent:
                self._clog("info", "profile.disconnect.ok",
                           f"DisconnectProfile({_uuid_label(uuid)}) on {device_address}",
                           address=device_address, uuid=str(uuid))
            return True
        except dbus.DBusException as e:
            logger.warning("DisconnectProfile(%s) on %s failed: %s",
                           uuid, device_address, e)
            if not silent:
                self._clog("debug", "profile.disconnect.fail",
                           f"DisconnectProfile({_uuid_label(uuid)}) on {device_address} "
                           f"failed: {e}",
                           address=device_address, uuid=str(uuid))
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

    def list_all_device_paths(self, device_address):
        """Return ``[(device_path, adapter_path), ...]`` for every BlueZ
        record of this address across every adapter.

        Used by :meth:`remove_device` to purge every copy — BlueZ keeps
        a separate ``Device1`` object per adapter, and a device that is
        paired on both adapters (or has stale state on one of them)
        won't look "gone" to the OS until all copies are removed.
        """
        results = []
        try:
            om = dbus.Interface(
                self._bus.get_object(BLUEZ_SERVICE, "/"), OM_IFACE
            )
            objects = om.GetManagedObjects()
            for path, interfaces in objects.items():
                if DEVICE_IFACE not in interfaces:
                    continue
                props = interfaces[DEVICE_IFACE]
                addr = str(props.get("Address", "")).upper()
                if addr == device_address.upper():
                    adapter_path = str(props.get("Adapter", ""))
                    results.append((str(path), adapter_path))
        except dbus.DBusException as e:
            logger.error("Failed to enumerate devices: %s", e)
        return results

    def remove_device(self, device_address, adapter_name=None):
        """Remove (unpair) a device, by default purging every BlueZ
        record of it on every adapter.

        Pass ``adapter_name`` to scope removal to a single adapter.
        Otherwise we nuke the device wherever BlueZ knows about it —
        that matches the user's mental model ("I removed it from the
        app, it should be gone everywhere") and stops the desktop
        Bluetooth applet from flickering connect/disconnect events
        because a stale bonding survived on another adapter.

        Before each ``Adapter1.RemoveDevice`` we call
        ``Device1.Disconnect`` so the ACL link is torn down first; if
        we skip that, the kernel can hold the link up long enough for
        a scanner to keep auto-reconnecting during the removal.
        """
        instances = self.list_all_device_paths(device_address)
        if adapter_name:
            suffix = "/" + adapter_name
            instances = [(p, a) for (p, a) in instances
                         if a.endswith(suffix)]
        if not instances:
            logger.error("Device %s not found for removal", device_address)
            self._clog("warn", "device.remove.not_found",
                       f"{device_address} not present in BlueZ "
                       f"(scope: {adapter_name or 'all adapters'})",
                       address=device_address)
            return False

        removed_any = False
        for device_path, adapter_path in instances:
            adapter_short = adapter_path.split("/")[-1] or adapter_path
            # Drop the ACL first so the kernel doesn't hold the link
            # up long enough for the scanner to auto-reconnect.
            try:
                device = dbus.Interface(
                    self._bus.get_object(BLUEZ_SERVICE, device_path),
                    DEVICE_IFACE,
                )
                try:
                    device.Disconnect()
                except dbus.DBusException:
                    # Not connected — nothing to tear down.
                    pass
            except dbus.DBusException:
                pass
            try:
                adapter = dbus.Interface(
                    self._bus.get_object(BLUEZ_SERVICE, adapter_path),
                    ADAPTER_IFACE,
                )
                adapter.RemoveDevice(device_path)
                logger.info("Removed device %s from %s",
                            device_address, adapter_short)
                self._clog("info", "device.remove",
                           f"Unpaired {device_address} from {adapter_short}",
                           address=device_address, adapter=adapter_short)
                removed_any = True
            except dbus.DBusException as e:
                logger.error("Failed to remove %s from %s: %s",
                             device_address, adapter_short, e)
                self._clog("warn", "device.remove.fail",
                           f"RemoveDevice({device_address}) on "
                           f"{adapter_short} failed: {e}",
                           address=device_address, adapter=adapter_short)
        return removed_any

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
