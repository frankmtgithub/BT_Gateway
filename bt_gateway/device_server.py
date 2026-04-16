"""Device SPP server using BlueZ Profile1 D-Bus API.

Registers an SPP (Serial Port Profile) service on the device adapter.
When a remote device connects, BlueZ hands us the RFCOMM file descriptor
through the Profile1.NewConnection callback.  A reader thread is spawned
for each connected device to forward data to the message router.

Because some devices (e.g. barcode scanners) also expose HID and will
default to Bluetooth keyboard input that opens URLs in the user's
browser, we actively disconnect the HID profile on connect so the data
flows only through SPP to our router.
"""

import logging
import os
import socket
import threading
import time

import dbus
import dbus.service

logger = logging.getLogger(__name__)

try:
    from socket import AF_BLUETOOTH, BTPROTO_RFCOMM
except ImportError:
    AF_BLUETOOTH = 31
    BTPROTO_RFCOMM = 3

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
PROFILE_PATH_BASE = "/org/bluez/btgateway/spp_profile"
RECV_BUFFER = 4096

# How often the auto-connect thread probes enabled paired devices that
# aren't currently connected.  Short enough that a scanner coming into
# range establishes its SPP link within a few seconds without the user
# having to click anything.
AUTO_CONNECT_INTERVAL = 10


class DeviceConnection:
    """Wraps a single RFCOMM connection to a remote device."""

    def __init__(self, address, sock, on_data, on_disconnect, on_raw=None):
        self.address = address
        self._sock = sock
        self._on_data = on_data
        self._on_disconnect = on_disconnect
        self._on_raw = on_raw
        self._running = False
        self._thread = None
        self._send_lock = threading.Lock()

    def start(self):
        self._running = True
        self._thread = threading.Thread(
            target=self._read_loop, daemon=True,
            name=f"device-{self.address}"
        )
        self._thread.start()

    def stop(self):
        self._running = False
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread:
            self._thread.join(timeout=3)

    def send(self, data):
        """Send raw data to the device (newline-terminated)."""
        with self._send_lock:
            try:
                payload = data if isinstance(data, str) else str(data)
                self._sock.sendall((payload + "\n").encode("utf-8"))
                return True
            except OSError as e:
                logger.error("Send to device %s failed: %s", self.address, e)
                return False

    def _read_loop(self):
        buffer = ""
        while self._running:
            try:
                data = self._sock.recv(RECV_BUFFER)
                if not data:
                    logger.info("Device %s disconnected (EOF)", self.address)
                    break
                decoded = data.decode("utf-8", errors="replace")
                # Debug / raw chunk notification
                if self._on_raw is not None:
                    try:
                        self._on_raw(self.address, decoded)
                    except Exception:
                        logger.exception("on_raw callback failed")
                buffer += decoded
                # Process complete lines (newline-delimited)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._on_data(self.address, line)
            except socket.timeout:
                continue
            except OSError as e:
                if self._running:
                    logger.error("Device %s read error: %s", self.address, e)
                break

        self._running = False
        self._on_disconnect(self.address)


class SPPProfile(dbus.service.Object):
    """BlueZ Profile1 implementation for the SPP server.

    One instance is registered per RFCOMM channel that at least one enabled
    paired device wants to listen on.  BlueZ calls NewConnection on the
    instance whose channel matches the one the remote device hit.
    """

    def __init__(self, bus, path, channel, owner):
        """``owner`` is the :class:`DeviceServer` that created us.  We route
        the accepted connections back to it so it can keep a single
        address → DeviceConnection map shared across every channel."""
        self._channel = int(channel)
        self._owner = owner
        super().__init__(bus, path)

    @property
    def channel(self):
        return self._channel

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device_path, fd, fd_properties):
        """Called by BlueZ when a device connects to our SPP service."""
        # Extract address from device path: /org/bluez/hciX/dev_AA_BB_CC_DD_EE_FF
        addr_part = device_path.split("/")[-1]
        if addr_part.startswith("dev_"):
            address = addr_part[4:].replace("_", ":").upper()
        else:
            address = addr_part

        logger.info("New device connection from %s on channel %d (path: %s)",
                    address, self._channel, device_path)
        self._owner._clog(
            "info", "spp.newconnection",
            f"Incoming RFCOMM from {address} on channel {self._channel}",
            address=address, channel=self._channel,
        )

        # Take ownership of the file descriptor
        if hasattr(fd, "take"):
            fd_num = fd.take()
        else:
            fd_num = int(fd)

        # Gate: is this connection allowed?  We check gating BEFORE turning
        # the fd into a socket so the reject path is a single close().
        reason = self._owner.check_connection_allowed(address, self._channel)
        if reason:
            logger.warning("Rejecting connection from %s on channel %d: %s",
                           address, self._channel, reason)
            self._owner._clog(
                "warn", "spp.rejected",
                f"Rejected {address} on channel {self._channel}: {reason}",
                address=address, channel=self._channel, reason=reason,
            )
            try:
                os.close(fd_num)
            except OSError:
                pass
            return

        # Create a socket from the file descriptor
        try:
            sock = socket.fromfd(fd_num, AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
            os.close(fd_num)  # fromfd dups the fd
        except OSError as e:
            logger.error("Failed to create socket from fd for %s: %s", address, e)
            try:
                os.close(fd_num)
            except OSError:
                pass
            return

        self._owner.accept_connection(address, sock)

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="o", out_signature="")
    def RequestDisconnection(self, device_path):
        addr_part = device_path.split("/")[-1]
        if addr_part.startswith("dev_"):
            address = addr_part[4:].replace("_", ":").upper()
        else:
            address = addr_part
        logger.info("Disconnection requested for %s (channel %d)",
                    address, self._channel)
        self._owner.disconnect_device(address)

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="", out_signature="")
    def Release(self):
        logger.info("SPP Profile on channel %d released by BlueZ",
                    self._channel)


class DeviceServer:
    """Manages the SPP server lifecycle: per-channel profile registration,
    device connection accounting, and pairing mode."""

    def __init__(self, config, router, bt_manager, socketio=None,
                 conn_log=None):
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._socketio = socketio
        self._conn_log = conn_log
        # channel (int) -> SPPProfile
        self._profiles = {}
        # BT address -> DeviceConnection
        self._connections = {}
        self._lock = threading.Lock()
        self._pairing_mode = False
        self._started = False
        # Background auto-connect thread bringing enabled paired devices
        # back onto SPP without user intervention (and killing HID so
        # scanners don't act as keyboards).
        self._auto_connect_thread = None
        self._auto_connect_running = False
        # HID→SPP handover state: address → threading.Event that stops
        # the handover keepalive thread for that device.
        self._handover_stops = {}
        self._handover_threads = {}

    def _clog(self, level, step, detail, **kw):
        if self._conn_log is None:
            return
        getattr(self._conn_log, level)(step, detail, **kw)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Power on the device adapter and register SPP profiles for every
        enabled paired device's listen channel."""
        adapter_name = self._config.get("device_adapter", "")
        if not adapter_name:
            logger.warning("No device adapter configured")
            return False

        self._bt_manager.power_adapter(adapter_name, True)
        self._started = True
        self.refresh_profiles()
        self._start_auto_connect()
        return True

    def stop(self):
        self._auto_connect_running = False
        self.disconnect_all()
        self._unregister_all_profiles()
        self.set_pairing_mode(False)
        self._started = False
        if self._auto_connect_thread:
            self._auto_connect_thread.join(timeout=3)
            self._auto_connect_thread = None

    # ── Profile registration (one per listen channel in use) ───────────

    def refresh_profiles(self):
        """Recompute the set of RFCOMM channels we need to listen on from
        the enabled-devices list and bring registrations into alignment.

        Always called after any change to devices, enabled flags, or
        listen channels so the BlueZ SDP record reflects reality.
        """
        if not self._started:
            return

        desired = set()
        for addr, dev in self._config.get_enabled_devices().items():
            if self._is_plc_paired_address(addr):
                # Never expose SPP for a device that's paired on the PLC
                # adapter — that device belongs to the PLC side.
                continue
            channel = int(dev.get("listen_channel") or 0)
            if 1 <= channel <= 30:
                desired.add(channel)

        with self._lock:
            current = set(self._profiles.keys())

        # Register any newly-needed channels
        for channel in sorted(desired - current):
            self._register_profile(channel)

        # Unregister channels we no longer need
        for channel in sorted(current - desired):
            self._unregister_profile(channel)

    def _register_profile(self, channel):
        path = f"{PROFILE_PATH_BASE}_ch{channel}"
        try:
            profile = SPPProfile(self._bt_manager.bus, path, channel, self)
            manager = dbus.Interface(
                self._bt_manager.bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.ProfileManager1",
            )
            opts = {
                "Name": dbus.String(f"BT Gateway SPP (ch{channel})"),
                "Role": dbus.String("server"),
                "Channel": dbus.UInt16(channel),
                "AutoConnect": dbus.Boolean(False),
                "RequireAuthentication": dbus.Boolean(False),
                "RequireAuthorization": dbus.Boolean(False),
            }
            manager.RegisterProfile(path, SPP_UUID, opts)
            with self._lock:
                self._profiles[channel] = profile
            logger.info("SPP profile registered on RFCOMM channel %d", channel)
            self._clog("info", "profile.register",
                       f"SPP listener armed on RFCOMM channel {channel}",
                       channel=channel)
        except dbus.DBusException as e:
            logger.error("Failed to register SPP profile on channel %d: %s",
                         channel, e)
            self._clog("error", "profile.register.fail",
                       f"RegisterProfile on channel {channel} failed: {e}",
                       channel=channel)

    def _unregister_profile(self, channel):
        with self._lock:
            profile = self._profiles.pop(channel, None)
        if profile is None:
            return
        path = f"{PROFILE_PATH_BASE}_ch{channel}"
        try:
            manager = dbus.Interface(
                self._bt_manager.bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.ProfileManager1",
            )
            manager.UnregisterProfile(path)
            logger.info("SPP profile unregistered on RFCOMM channel %d", channel)
            self._clog("info", "profile.unregister",
                       f"SPP listener torn down on channel {channel}",
                       channel=channel)
        except dbus.DBusException as e:
            logger.warning("UnregisterProfile on channel %d failed: %s",
                           channel, e)
            self._clog("warn", "profile.unregister.fail",
                       f"UnregisterProfile on channel {channel} failed: {e}",
                       channel=channel)
        try:
            profile.remove_from_connection()
        except Exception:
            pass

    def _unregister_all_profiles(self):
        with self._lock:
            channels = list(self._profiles.keys())
        for channel in channels:
            self._unregister_profile(channel)

    # ── Connection gate (called by SPPProfile.NewConnection) ───────────

    def check_connection_allowed(self, address, channel):
        """Return None if the device is allowed to connect on this channel,
        otherwise a short human-readable reason string.

        Enforces a per-channel exclusivity rule: each RFCOMM channel is
        owned by exactly one paired address.  A channel advertised for
        device A is closed to every other address, even if they are
        paired.  That way a second scanner accidentally set to the same
        SPP channel can't pre-empt the real owner.
        """
        if self._is_plc_paired_address(address):
            return "device is paired on PLC adapter"
        devices = self._config.get_devices()
        dev = devices.get(address)
        if dev is None:
            return "device not paired with this gateway"
        if not dev.get("enabled", True):
            return "device is disabled"
        configured = int(dev.get("listen_channel") or 0)
        if configured and configured != channel:
            return (f"device configured for channel {configured}, "
                    f"got {channel}")

        # Reject if another enabled paired device claims this channel.
        # This is a runtime defence; config-level uniqueness is also
        # enforced in Config.set_device_listen_channel.
        for other_addr, other_dev in devices.items():
            if other_addr.upper() == address.upper():
                continue
            if not other_dev.get("enabled", True):
                continue
            if int(other_dev.get("listen_channel") or 0) == int(channel):
                return (f"channel {channel} is owned by another device "
                        f"({other_addr})")
        return None

    def accept_connection(self, address, sock):
        """Finalise an accepted SPP connection from a remote device."""
        # Auto-register the device if it isn't in config yet.  (Shouldn't
        # happen — check_connection_allowed rejects unknowns — but keep as
        # a safety net so we never drop data.)
        device_entry = self._config.add_device(address)
        device_name = device_entry["name"] if isinstance(device_entry, dict) \
            else device_entry

        self._clog("info", "spp.accept",
                   f"SPP accepted from {device_name} ({address})",
                   address=address)

        # A successful SPP connection cancels any in-flight handover for
        # this device — we got what we were waiting for.
        self._stop_handover_unlocked(address, reason="spp.connected")

        # HID scanners announce both SPP and HID.  BlueZ will usually bring
        # up both, meaning scan events are sent to the OS as keystrokes
        # (which is why scanning opens a browser URL).  Disconnect HID so
        # the data stays on our SPP channel only.
        adapter_name = self._config.get("device_adapter", "")
        self._bt_manager.disconnect_profile(address, HID_UUID, adapter_name)

        conn = DeviceConnection(
            address=address,
            sock=sock,
            on_data=self._on_device_data,
            on_disconnect=self._on_device_disconnect,
            on_raw=self._on_device_raw,
        )

        with self._lock:
            old_conn = self._connections.get(address)
            if old_conn:
                logger.info("Replacing existing connection from %s", address)
                old_conn.stop()
            self._connections[address] = conn

        self._router.register_device(address, conn)
        conn.start()

        logger.info("Device %s (%s) connected and receiving", address, device_name)
        if self._socketio:
            self._socketio.emit("device_connected", {
                "address": address,
                "name": device_name,
            }, namespace="/")

    # ── Connection accounting ──────────────────────────────────────────

    def _on_device_data(self, address, data):
        self._router.route_from_device(address, data)

    def _on_device_raw(self, address, raw_chunk):
        self._router.notify_device_raw(address, raw_chunk)

    def _on_device_disconnect(self, address):
        self.disconnect_device(address)

    def disconnect_device(self, address):
        with self._lock:
            conn = self._connections.pop(address, None)
        if conn:
            conn.stop()
        self._router.unregister_device(address)
        device_name = self._config.get_device_name(address) or address
        logger.info("Device %s (%s) disconnected", address, device_name)
        self._clog("info", "spp.disconnect",
                   f"SPP disconnected: {device_name} ({address})",
                   address=address)
        if self._socketio:
            self._socketio.emit("device_disconnected", {
                "address": address,
                "name": device_name,
            }, namespace="/")

    def disconnect_all(self):
        with self._lock:
            addresses = list(self._connections.keys())
        for addr in addresses:
            self.disconnect_device(addr)

    def get_active_connections(self):
        with self._lock:
            return list(self._connections.keys())

    # ── Misc ───────────────────────────────────────────────────────────

    def _is_plc_paired_address(self, address):
        """True if ``address`` is the single PLC-paired device on the PLC
        adapter.  Used to lock this device out of the devices side entirely."""
        plc_adapter = self._config.get("plc_adapter", "")
        if not plc_adapter:
            return False
        try:
            plc_dev = self._bt_manager.get_single_paired_device(plc_adapter)
        except Exception:
            return False
        if not plc_dev:
            return False
        return plc_dev.get("address", "").upper() == address.upper()

    # ── Auto-connect loop ──────────────────────────────────────────────

    def _start_auto_connect(self):
        """Spawn a background thread that periodically nudges enabled
        paired devices onto SPP when they aren't currently connected.

        Scanners that are configured for SPP mode typically wait for the
        host (the Pi) to initiate the RFCOMM connection rather than doing
        it themselves, so calling BlueZ ``ConnectProfile(SPP)`` from our
        side is what brings the link up without the user clicking
        "connect" in the desktop Bluetooth applet.  We also hammer
        ``DisconnectProfile(HID)`` so a scanner that's still in keyboard
        mode doesn't leave stray HID state connected on the gateway.
        """
        if self._auto_connect_thread and self._auto_connect_thread.is_alive():
            return
        self._auto_connect_running = True
        self._auto_connect_thread = threading.Thread(
            target=self._auto_connect_loop,
            daemon=True,
            name="device-auto-connect",
        )
        self._auto_connect_thread.start()

    def _auto_connect_loop(self):
        logger.info("Device auto-connect thread started (interval %ds)",
                    AUTO_CONNECT_INTERVAL)
        while self._auto_connect_running:
            try:
                self._auto_connect_tick()
            except Exception:
                logger.exception("auto-connect tick failed")
            # Interruptible sleep
            deadline = time.monotonic() + AUTO_CONNECT_INTERVAL
            while self._auto_connect_running and time.monotonic() < deadline:
                time.sleep(0.5)
        logger.info("Device auto-connect thread stopped")

    def _auto_connect_tick(self):
        """One iteration of the auto-connect loop — called on a schedule."""
        adapter_name = self._config.get("device_adapter", "")
        if not adapter_name:
            return

        # Snapshot the connected set under lock, then work outside it.
        with self._lock:
            connected = set(self._connections.keys())
            in_handover = set(self._handover_stops.keys())

        for addr, dev in self._config.get_enabled_devices().items():
            if self._is_plc_paired_address(addr):
                continue
            if addr in connected:
                # Already happily streaming — just keep HID off in case
                # BlueZ opportunistically reopened it in the background.
                self._bt_manager.disconnect_profile(addr, HID_UUID, adapter_name)
                continue
            if addr in in_handover:
                # A dedicated thread is driving this device right now.
                # Don't race it.
                continue
            # Try to bring SPP up.  Belt-and-suspenders: drop HID first so
            # BlueZ doesn't "already-connected" us via the wrong profile.
            logger.debug("Auto-connect: attempting SPP on %s", addr)
            self._clog("debug", "auto.tick",
                       f"Auto-connect: dropping HID and asking for SPP on {addr}",
                       address=addr,
                       channel=int(dev.get("listen_channel") or 0) or None)
            self._bt_manager.disconnect_profile(addr, HID_UUID, adapter_name)
            self._bt_manager.connect_profile(addr, SPP_UUID, adapter_name)

    # ── HID → SPP handover ─────────────────────────────────────────────

    def start_handover(self, address):
        """Begin the HID→SPP handover flow for ``address``.

        The user pairs a scanner in HID (keyboard) mode, then scans the
        vendor "switch to SPP" barcode.  For that transition to land on
        our SPP listener reliably, two things need to be true at the
        instant the scanner switches:

        1. The ACL link to the scanner is already up.  We force that by
           calling ``Device1.Connect`` which pulls HID up.
        2. Our SPP profile is registered on the device's ``listen_channel``.
           We call :meth:`refresh_profiles` to be sure.

        After that we spawn a short-lived keepalive thread that re-asserts
        both conditions every 2 s for a handover window (default 90 s),
        logs everything, and bails out as soon as SPP actually connects
        (``accept_connection`` calls :meth:`_stop_handover_unlocked`).
        """
        address = address.upper()
        devices = self._config.get_devices()
        dev = devices.get(address)
        if dev is None:
            self._clog("warn", "handover.unknown",
                       f"start_handover called for unknown device {address}",
                       address=address)
            return False
        if self._is_plc_paired_address(address):
            self._clog("warn", "handover.plc_lockout",
                       f"{address} belongs to the PLC adapter; refusing handover",
                       address=address)
            return False
        channel = int(dev.get("listen_channel") or 0)

        with self._lock:
            if address in self._handover_stops:
                self._clog("info", "handover.already",
                           f"Handover already running for {address}",
                           address=address, channel=channel)
                return True
            stop = threading.Event()
            self._handover_stops[address] = stop

        # Make sure the SPP listener for this device's channel is live
        # BEFORE we touch the scanner.  Scanners often flip to SPP within
        # 1–2 s of the barcode scan, so the listener must be ready first.
        self.refresh_profiles()

        self._clog("info", "handover.start",
                   f"Handover armed for {address} on channel {channel}. "
                   "Bringing HID up now; scan the SPP-mode barcode.",
                   address=address, channel=channel)

        thread = threading.Thread(
            target=self._handover_loop,
            args=(address, channel, stop),
            daemon=True,
            name=f"handover-{address}",
        )
        with self._lock:
            self._handover_threads[address] = thread
        thread.start()
        return True

    def stop_handover(self, address, reason="user.cancel"):
        """Cancel any in-flight handover for ``address``."""
        address = address.upper()
        self._stop_handover_unlocked(address, reason=reason)

    def _stop_handover_unlocked(self, address, reason="unknown"):
        with self._lock:
            stop = self._handover_stops.pop(address, None)
            thread = self._handover_threads.pop(address, None)
        if stop is None:
            return
        stop.set()
        self._clog("info", "handover.stop",
                   f"Handover for {address} stopped ({reason})",
                   address=address, reason=reason)
        # Don't join from callers that may be inside the thread itself
        # (e.g. accept_connection).  Daemon threads exit on their own.

    def _handover_loop(self, address, channel, stop):
        adapter = self._config.get("device_adapter", "")
        deadline = time.monotonic() + 90.0  # 90s handover window

        # Kick: force the ACL link up.  Scanners in HID mode will come up
        # as a keyboard; that's fine — we just need the link alive.
        if self._bt_manager.connect_device(address, adapter):
            self._clog("info", "handover.hid_up",
                       f"ACL link to {address} is up. SPP listener armed "
                       f"on channel {channel}. "
                       "Scan the 'switch to SPP' barcode now.",
                       address=address, channel=channel)
        else:
            self._clog("warn", "handover.hid_fail",
                       f"Could not bring {address} up via Device1.Connect. "
                       "Is the scanner in range and powered on?",
                       address=address, channel=channel)

        # Keepalive: re-register the SPP profile and nudge BlueZ every
        # 2 s.  Bail out if someone called stop_handover (which fires when
        # SPP accepts, when the user cancels, or when the window elapses).
        tick = 0
        while not stop.is_set() and time.monotonic() < deadline:
            tick += 1
            # The profile may have been torn down if the user flipped
            # another device's enabled flag — refresh to be sure.
            self.refresh_profiles()
            if tick % 5 == 0:
                still_up = self._bt_manager.is_device_connected(address, adapter)
                self._clog(
                    "debug", "handover.keepalive",
                    f"Handover keepalive tick {tick}: ACL "
                    f"{'up' if still_up else 'down'}, "
                    f"SPP listener on channel {channel} active.",
                    address=address, channel=channel,
                )
                if not still_up:
                    # The scanner may have dropped HID to switch modes.
                    # Try to re-pull it up so it doesn't sleep during the
                    # transition window.
                    self._bt_manager.connect_device(address, adapter)
            if stop.wait(2.0):
                break

        # If we fell out of the loop without SPP arriving, say so plainly.
        with self._lock:
            still_running = address in self._handover_stops
        if still_running:
            self._clog(
                "warn", "handover.timeout",
                f"Handover window expired for {address} without an SPP "
                f"connection on channel {channel}. Check that the scanner "
                "actually switched modes and that no other device is "
                "claiming this channel.",
                address=address, channel=channel,
            )
            self._stop_handover_unlocked(address, reason="timeout")

    @property
    def active_handovers(self):
        with self._lock:
            return sorted(self._handover_stops.keys())

    def set_pairing_mode(self, enabled, adapter_name=None):
        """Enable or disable pairing mode (discoverable + pairable).

        ``adapter_name`` overrides the configured device adapter when provided.
        """
        if not adapter_name:
            adapter_name = self._config.get("device_adapter", "")
        if not adapter_name:
            return False
        self._bt_manager.set_discoverable(adapter_name, enabled)
        self._bt_manager.set_pairable(adapter_name, enabled)
        self._pairing_mode = enabled
        logger.info(
            "Pairing mode %s on %s",
            "enabled" if enabled else "disabled",
            adapter_name,
        )
        if self._socketio:
            self._socketio.emit(
                "pairing_mode",
                {"enabled": enabled, "adapter": adapter_name},
                namespace="/",
            )
        return True

    @property
    def pairing_mode(self):
        return self._pairing_mode

    @property
    def listening_channels(self):
        with self._lock:
            return sorted(self._profiles.keys())
