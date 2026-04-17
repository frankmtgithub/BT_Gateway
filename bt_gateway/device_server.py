"""Device-side gateway: one outbound RFCOMM client per paired scanner.

Mirrors :mod:`bt_gateway.plc_connection` on the devices side.  For every
enabled paired scanner we run a dedicated manager thread that

1. binds ``/dev/rfcomm<N>`` to the scanner's BT address + RFCOMM channel,
   and
2. opens that TTY — which is what actually triggers the kernel to dial
   the scanner over RFCOMM (exactly the way Hercules opening COM6 on
   Windows triggers Windows's BT stack to dial the remote SPP server).

While the TTY is open we read lines off it and push them to the router;
when the link drops the manager releases the TTY fd, waits a backoff
interval, and tries again.  Scanners that are still in HID-only mode
(post-pair, pre-barcode-switch) just fail to dial and get retried quietly
until the operator scans the vendor "switch to SPP" setup barcode.

This replaces the old Profile1 / ``NewConnection`` server path.  The Pi
is now the RFCOMM *client* for both the PLC and the scanners; the only
difference is that the PLC is one device on the PLC adapter, while the
scanners are N devices on the devices adapter.
"""

import logging
import os
import termios
import threading
import time
import tty

from bt_gateway import rfcomm_tty

logger = logging.getLogger(__name__)

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

# How long to wait between reconnect attempts on a scanner that is
# currently unreachable (powered off, out of range, still in HID mode,
# etc.).  The backoff ladder matches the PLC reconnect cadence so every
# outbound link on this Pi behaves the same.
DIAL_BACKOFF = [5, 10, 20, 40, 60]

# TTY read timeout — the manager wakes up this often to check for
# shutdown even if no data is arriving.
READ_TIMEOUT = 1.0

# While the user is pairing (or has just paired), pause every outbound
# dial so BlueZ's D-Bus channel has exclusive access.
DEFAULT_PAIR_GUARD_SECONDS = 90.0


class _DeviceLink:
    """One outbound RFCOMM-over-TTY connection to a single scanner.

    Owns the lifetime of ``/dev/rfcomm<port>`` for this device: binds it
    on start, opens it in a loop (dialing), reads lines, notifies the
    router, and releases the binding on stop.
    """

    def __init__(self, address, port, channel, adapter_name,
                 config, router, bt_manager, conn_log, socketio):
        self.address = address.upper()
        self._port = int(port)
        self._channel = int(channel) if channel else 0
        self._adapter_name = adapter_name
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._conn_log = conn_log
        self._socketio = socketio

        self._fd = None
        self._fd_lock = threading.Lock()
        self._thread = None
        self._running = False
        self._registered = False
        self._fail_count = 0
        self._next_attempt = 0.0

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name=f"device-link-{self.address}",
        )
        self._thread.start()

    def stop(self):
        self._running = False
        self._close_fd()
        rfcomm_tty.release(self._port)
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None
        self._unregister_from_router()

    # ── Router-facing send ─────────────────────────────────────────────

    def send(self, data):
        """Write newline-terminated ``data`` out to the scanner."""
        with self._fd_lock:
            fd = self._fd
            if fd is None:
                return False
            try:
                payload = data if isinstance(data, str) else str(data)
                os.write(fd, (payload + "\n").encode("utf-8"))
                return True
            except OSError as e:
                logger.error("Write to %s failed: %s", self.address, e)
                return False

    @property
    def port(self):
        return self._port

    @property
    def channel(self):
        return self._channel

    @property
    def is_connected(self):
        return self._fd is not None

    # ── Main loop ──────────────────────────────────────────────────────

    def _run(self):
        # Discover the scanner's actual SPP channel once before we start
        # dialing.  The per-device listen_channel in config is used only
        # as an override when SDP fails.
        resolved_channel = self._resolve_channel()
        if not resolved_channel:
            self._clog("warn", "device.dial.no_channel",
                       f"{self.address}: no SPP channel known yet, will "
                       "keep retrying.  Switch the scanner to SPP mode.",
                       address=self.address)

        while self._running:
            now = time.monotonic()
            if now < self._next_attempt:
                time.sleep(min(1.0, self._next_attempt - now))
                continue

            # If the scanner has been set to pair-guard or disabled mid-run,
            # the outer DeviceServer will have called stop() on us already.
            channel = resolved_channel or self._channel
            if not channel:
                # Keep probing SDP in case the scanner finally advertises
                # SPP (firmware mode change), but do it slowly.
                self._schedule_retry()
                continue

            # Make sure the TTY binding matches our target and exists.
            if not rfcomm_tty.bind(self._port, self.address, channel):
                self._clog("warn", "rfcomm.bind_fail",
                           f"rfcomm bind /dev/rfcomm{self._port} → "
                           f"{self.address} ch{channel} failed",
                           address=self.address, channel=channel)
                self._schedule_retry()
                continue

            # `rfcomm bind` reports success at the kernel level even when
            # the resulting /dev node is invisible to us — this happens
            # inside a Docker container whose /dev tmpfs is not
            # bind-mounted from the host.  Surface it clearly instead of
            # letting the operator chase cryptic ENOENT errors on open().
            if not rfcomm_tty.tty_exists(self._port):
                self._clog("error", "rfcomm.tty_missing",
                           f"rfcomm bind succeeded but /dev/rfcomm"
                           f"{self._port} does not exist.  If running "
                           "under Docker, mount the host's /dev into the "
                           "container (add '- /dev:/dev' to volumes).",
                           address=self.address, channel=channel)
                self._schedule_retry()
                continue

            # Trust the device so BlueZ doesn't pop an authorisation prompt
            # on every dial.
            try:
                self._bt_manager.set_device_trusted(
                    self.address, True, self._adapter_name,
                )
            except Exception:
                pass

            self._clog("info", "device.dial",
                       f"Dialing /dev/rfcomm{self._port} → {self.address} "
                       f"ch{channel}",
                       address=self.address, channel=channel)

            fd = self._open_tty()
            if fd is None:
                # Dial failed — scanner asleep, out of range, or still in
                # HID-only mode.  Back off and retry.
                self._register_failure(channel)
                continue

            # Dial succeeded — we have a live RFCOMM link.
            self._fail_count = 0
            self._next_attempt = 0.0
            with self._fd_lock:
                self._fd = fd

            self._register_with_router()
            self._clog("info", "device.connected",
                       f"{self.address} connected on /dev/rfcomm{self._port} "
                       f"(channel {channel})",
                       address=self.address, channel=channel)
            self._emit_connected()

            # Opportunistic: a scanner that just accepted SPP often still
            # has a dangling HID profile from pairing.  Drop it so the Pi
            # doesn't also receive scans as keystrokes.
            try:
                self._bt_manager.disconnect_profile(
                    self.address, HID_UUID, self._adapter_name,
                )
            except Exception:
                pass

            try:
                self._read_loop(fd)
            finally:
                self._close_fd()
                self._unregister_from_router()
                self._emit_disconnected()
                self._clog("info", "device.disconnected",
                           f"{self.address} disconnected "
                           f"from /dev/rfcomm{self._port}",
                           address=self.address, channel=channel)

            # Brief pause before redialing so we don't spin on a flapping
            # link.  If the link was healthy and just closed we reconnect
            # quickly; repeated failures will re-escalate the backoff.
            if self._running:
                time.sleep(1.0)

        # Clean exit.
        rfcomm_tty.release(self._port)

    def _resolve_channel(self):
        """Return the RFCOMM channel to dial on the scanner.

        Preference order: live SDP lookup → per-device configured
        ``listen_channel`` override → None.
        """
        try:
            ch = self._bt_manager.sdp_find_spp_channel(self.address)
        except Exception:
            ch = None
        if ch:
            if ch != self._channel:
                self._clog("info", "device.sdp",
                           f"SDP: {self.address} advertises SPP on "
                           f"channel {ch}",
                           address=self.address, channel=ch)
            return int(ch)
        if self._channel:
            self._clog("debug", "device.sdp.fallback",
                       f"SDP failed for {self.address}, using configured "
                       f"channel {self._channel}",
                       address=self.address, channel=self._channel)
            return int(self._channel)
        return None

    def _open_tty(self):
        """Open ``/dev/rfcomm<port>`` and put it into raw mode.

        The open is blocking, which means the kernel completes the
        RFCOMM dial before returning the fd — the same way
        ``CreateFile("COM6")`` on Windows waits for the underlying
        Bluetooth link to come up.  If the scanner is unreachable the
        kernel raises after ~20 s with ``EHOSTDOWN`` /
        ``ETIMEDOUT`` / ``ECONNREFUSED`` and we fall into the backoff
        ladder.
        """
        path = rfcomm_tty.device_path(self._port)
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOCTTY)
        except OSError as e:
            self._clog("debug", "device.dial.fail",
                       f"open({path}) failed: {e}",
                       address=self.address)
            return None

        try:
            tty.setraw(fd)
        except (OSError, termios.error) as e:
            logger.warning("Configuring TTY on %s failed: %s", path, e)
            try:
                os.close(fd)
            except OSError:
                pass
            return None

        return fd

    def _read_loop(self, fd):
        """Read newline-delimited data from the TTY until it closes."""
        buffer = ""
        while self._running:
            try:
                chunk = os.read(fd, 4096)
            except BlockingIOError:
                time.sleep(0.05)
                continue
            except OSError as e:
                if self._running:
                    logger.info("%s read error: %s", self.address, e)
                break
            if not chunk:
                logger.info("%s EOF on /dev/rfcomm%d",
                            self.address, self._port)
                break

            decoded = chunk.decode("utf-8", errors="replace")
            try:
                self._router.notify_device_raw(self.address, decoded)
            except Exception:
                logger.exception("notify_device_raw failed for %s",
                                 self.address)
            buffer += decoded
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                line = line.strip()
                if line:
                    try:
                        self._router.route_from_device(self.address, line)
                    except Exception:
                        logger.exception("route_from_device failed for %s",
                                         self.address)

    def _close_fd(self):
        with self._fd_lock:
            fd = self._fd
            self._fd = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    # ── Router registration ────────────────────────────────────────────

    def _register_with_router(self):
        if self._registered:
            return
        try:
            self._router.register_device(self.address, self)
            self._registered = True
        except Exception:
            logger.exception("register_device failed for %s", self.address)

    def _unregister_from_router(self):
        if not self._registered:
            return
        try:
            self._router.unregister_device(self.address)
        except Exception:
            logger.exception("unregister_device failed for %s", self.address)
        self._registered = False

    # ── Backoff ────────────────────────────────────────────────────────

    def _register_failure(self, channel):
        self._fail_count += 1
        idx = min(self._fail_count - 1, len(DIAL_BACKOFF) - 1)
        wait = DIAL_BACKOFF[idx]
        self._next_attempt = time.monotonic() + wait
        self._clog("debug", "device.dial.backoff",
                   f"Dial to {self.address} failed "
                   f"{self._fail_count}×; next attempt in {wait}s",
                   address=self.address, channel=channel)

    def _schedule_retry(self):
        self._next_attempt = time.monotonic() + DIAL_BACKOFF[0]

    # ── Helpers ────────────────────────────────────────────────────────

    def _emit_connected(self):
        if not self._socketio:
            return
        name = self._config.get_device_name(self.address) or self.address
        self._socketio.emit("device_connected", {
            "address": self.address,
            "name": name,
        }, namespace="/")

    def _emit_disconnected(self):
        if not self._socketio:
            return
        name = self._config.get_device_name(self.address) or self.address
        self._socketio.emit("device_disconnected", {
            "address": self.address,
            "name": name,
        }, namespace="/")

    def _clog(self, level, step, detail, **kw):
        if self._conn_log is None:
            return
        getattr(self._conn_log, level)(step, detail, **kw)


class DeviceServer:
    """Tracks one outbound RFCOMM link per enabled paired scanner.

    Lifecycle is driven by the config — :meth:`refresh_managers` brings
    the running set of links into alignment with the enabled-devices
    list.  Each link is a :class:`_DeviceLink` running in its own thread
    and talking to the router via ``register_device`` /
    ``route_from_device`` just like the old server path did.
    """

    def __init__(self, config, router, bt_manager, socketio=None,
                 conn_log=None):
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._socketio = socketio
        self._conn_log = conn_log
        # BT address -> _DeviceLink
        self._links = {}
        self._lock = threading.Lock()
        self._pairing_mode = False
        self._started = False
        self._pair_guard_until = 0.0

    def _clog(self, level, step, detail, **kw):
        if self._conn_log is None:
            return
        getattr(self._conn_log, level)(step, detail, **kw)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def start(self):
        """Power on the device adapter, clear stale RFCOMM bindings, and
        start a _DeviceLink for every enabled paired device."""
        adapter_name = self._config.get("device_adapter", "")
        if not adapter_name:
            logger.warning("No device adapter configured")
            return False

        if not rfcomm_tty.have_rfcomm():
            logger.error(
                "The 'rfcomm' utility is not installed; per-device TTYs "
                "cannot be created.  Install bluez-utils."
            )
            self._clog("error", "rfcomm.missing",
                       "rfcomm(1) not found on PATH; install bluez-utils")
            return False

        # Nuke any leftover bindings from a previous run so we don't
        # accidentally keep a scanner pinned to the wrong channel.
        rfcomm_tty.release_all()

        self._bt_manager.power_adapter(adapter_name, True)
        self._started = True
        self.refresh_managers()
        return True

    def stop(self):
        self._started = False
        self.disconnect_all()
        self.set_pairing_mode(False)
        rfcomm_tty.release_all()

    # ── Manager set ────────────────────────────────────────────────────

    def refresh_managers(self):
        """Bring the running set of links into alignment with the current
        enabled-devices config."""
        if not self._started:
            return

        adapter_name = self._config.get("device_adapter", "")
        desired = {}
        for addr, dev in self._config.get_enabled_devices().items():
            if self._is_plc_paired_address(addr):
                continue
            port = dev.get("port")
            if port is None:
                # Deterministic /dev/rfcomm<N> is core to this design —
                # the config auto-assigns one at pair time, so it should
                # always be set.  Skip if it's somehow missing.
                self._clog("warn", "device.skip.no_port",
                           f"Skipping {addr}: no /dev/rfcomm port assigned",
                           address=addr)
                continue
            channel = int(dev.get("listen_channel") or 0)
            desired[addr.upper()] = (int(port), channel)

        with self._lock:
            current = set(self._links.keys())
        desired_addrs = set(desired.keys())

        # Stop links that are no longer wanted or whose target changed.
        for addr in sorted(current):
            old = self._links[addr]
            if addr not in desired_addrs:
                self._stop_link(addr)
                continue
            port, channel = desired[addr]
            if old.port != port or old.channel != channel:
                self._stop_link(addr)

        # (Re)start links that should be running.
        for addr, (port, channel) in desired.items():
            with self._lock:
                already = addr in self._links
            if already:
                continue
            self._start_link(addr, port, channel, adapter_name)

    def _start_link(self, address, port, channel, adapter_name):
        link = _DeviceLink(
            address=address,
            port=port,
            channel=channel,
            adapter_name=adapter_name,
            config=self._config,
            router=self._router,
            bt_manager=self._bt_manager,
            conn_log=self._conn_log,
            socketio=self._socketio,
        )
        with self._lock:
            self._links[address] = link
        self._clog("info", "device.manager.start",
                   f"Manager started for {address} on /dev/rfcomm{port} "
                   f"(channel {channel or 'auto'})",
                   address=address, channel=channel)
        link.start()

    def _stop_link(self, address):
        with self._lock:
            link = self._links.pop(address, None)
        if link is None:
            return
        link.stop()
        self._clog("info", "device.manager.stop",
                   f"Manager stopped for {address}",
                   address=address)

    # ── Disconnect surface used by routes ──────────────────────────────

    def disconnect_device(self, address):
        """Force a disconnect on a specific device.  The link's manager
        thread will immediately attempt to redial, so this is really just
        a 'reset the TTY' button from the UI's perspective."""
        address = address.upper()
        with self._lock:
            link = self._links.get(address)
        if link is None:
            return
        link._close_fd()  # causes _read_loop to break
        link._unregister_from_router()

    def disconnect_all(self):
        with self._lock:
            addrs = list(self._links.keys())
        for addr in addrs:
            self._stop_link(addr)

    def get_active_connections(self):
        with self._lock:
            return [a for a, l in self._links.items() if l.is_connected]

    # ── Pairing mode ───────────────────────────────────────────────────

    def set_pairing_mode(self, enabled, adapter_name=None):
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

    # ── Pair guard ─────────────────────────────────────────────────────

    def begin_pair_guard(self, seconds=DEFAULT_PAIR_GUARD_SECONDS):
        """Suspend redial activity briefly so a user-initiated pair has
        clean D-Bus access.  Idempotent."""
        deadline = time.monotonic() + max(1.0, float(seconds))
        if deadline > self._pair_guard_until:
            self._pair_guard_until = deadline
            self._clog("info", "auto.pair_guard",
                       f"Outbound dials paused for {int(seconds)}s "
                       "while pairing runs")

    def end_pair_guard(self):
        if self._pair_guard_until:
            self._pair_guard_until = 0.0
            self._clog("info", "auto.pair_guard_off",
                       "Outbound dials resumed (pair guard cleared)")

    # ── Helpers ────────────────────────────────────────────────────────

    def _is_plc_paired_address(self, address):
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

    # ── Back-compat ────────────────────────────────────────────────────

    # Routes still call refresh_profiles() after config edits; keep the
    # old name as an alias so we don't have to touch every caller.
    def refresh_profiles(self):
        self.refresh_managers()

    @property
    def listening_channels(self):
        """Return the list of currently-bound RFCOMM channels (one per
        active link).  Exposed for the UI."""
        with self._lock:
            return sorted({l.channel for l in self._links.values() if l.channel})
