"""Device-side gateway: hybrid RFCOMM client / server per paired scanner.

Scanners ship in two very different SPP personalities:

* **SPP Slave** (registers an SDP SerialPort record): the host dials out
  to the scanner on its advertised channel.
* **SPP Master / Cradle** (no SDP record, scanner initiates): the host
  listens on an SPP channel and the scanner dials in whenever a barcode
  is triggered.

We don't know which mode a given scanner is in until it actually
connects, so each enabled device gets a :class:`_DeviceLink` that

1. binds ``/dev/rfcomm<N>`` to the scanner's BT address + RFCOMM channel
   and tries to **dial out** by opening the TTY (same path Hercules uses
   on Windows when it opens an outgoing COM port), and in parallel
2. is wired into a shared :class:`_SPPListener` that **accepts inbound**
   RFCOMM connections on the scanner's configured channel and routes
   them back to the right link by peer address.

Whichever path succeeds first wins.  The read loop is the same either
way — ``os.read`` on the fd, split on newlines, push to the router.
"""

import logging
import os
import queue
import socket
import termios
import threading
import time
import tty

import dbus
import dbus.service

from bt_gateway import rfcomm_tty

try:
    from socket import AF_BLUETOOTH, BTPROTO_RFCOMM
except ImportError:
    AF_BLUETOOTH = 31
    BTPROTO_RFCOMM = 3

logger = logging.getLogger(__name__)

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"

# How long to wait between reconnect attempts on a scanner that is
# currently unreachable (powered off, out of range, still in HID mode,
# etc.).  The backoff ladder matches the PLC reconnect cadence so every
# outbound link on this Pi behaves the same.
DIAL_BACKOFF = [5, 10, 20, 40, 60]

# Maximum time to wait for a single dial to complete.  We open the TTY
# non-blocking and poll() for it to become writable; if the remote
# doesn't answer within this window we treat it as unreachable and fall
# into the backoff ladder.  Without this a scanner that pairs but never
# brings up SPP (e.g. HID-only firmware, asleep post-pair) would hang
# the open() indefinitely.
DIAL_TIMEOUT = 15.0

# TTY read timeout — the manager wakes up this often to check for
# shutdown even if no data is arriving.
READ_TIMEOUT = 1.0

# While the user is pairing (or has just paired), pause every outbound
# dial so BlueZ's D-Bus channel has exclusive access.
DEFAULT_PAIR_GUARD_SECONDS = 90.0


_PROFILE_PATH_BASE = "/org/bluez/bt_gateway/spp"


class _SPPListener(dbus.service.Object):
    """Registers a BlueZ Profile1 in server role on one RFCOMM channel.

    SPP-Master scanners do an SDP SerialPort lookup on the host *before*
    dialing in — if the Pi doesn't publish an SDP record the scanner
    can't discover our channel and never connects.  BlueZ's Profile1
    registration both publishes that SDP record and delivers accepted
    connections to us via ``NewConnection`` (no standalone ``sdptool``
    needed, which doesn't work out-of-the-box on modern BlueZ anyway).

    One listener is shared across every enabled scanner that uses the
    same ``listen_channel``.  Each ``NewConnection`` is dispatched to
    the owning :class:`_DeviceLink` by peer BT address.
    """

    def __init__(self, channel, dispatch, bus, conn_log=None):
        self._channel = int(channel)
        self._dispatch = dispatch
        self._bus = bus
        self._conn_log = conn_log
        self._path = f"{_PROFILE_PATH_BASE}_ch{self._channel}"
        self._registered = False
        super().__init__(bus, self._path)

    @property
    def channel(self):
        return self._channel

    def start(self):
        try:
            manager = dbus.Interface(
                self._bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.ProfileManager1",
            )
            opts = {
                "Name": dbus.String(
                    f"BT Gateway SPP (ch{self._channel})"),
                "Role": dbus.String("server"),
                "Channel": dbus.UInt16(self._channel),
                "Service": dbus.String(SPP_UUID),
                "AutoConnect": dbus.Boolean(False),
                "RequireAuthentication": dbus.Boolean(False),
                "RequireAuthorization": dbus.Boolean(False),
            }
            manager.RegisterProfile(self._path, SPP_UUID, opts)
            self._registered = True
            self._clog("info", "listener.start",
                       f"SPP profile registered on RFCOMM channel "
                       f"{self._channel}; SDP SerialPort record "
                       "published so scanners can discover us",
                       channel=self._channel)
            return True
        except dbus.DBusException as e:
            logger.error("RegisterProfile on ch%d failed: %s",
                         self._channel, e)
            self._clog("error", "listener.start_fail",
                       f"RegisterProfile on channel {self._channel} "
                       f"failed: {e}", channel=self._channel)
            try:
                self.remove_from_connection()
            except Exception:
                pass
            return False

    def stop(self):
        if self._registered:
            try:
                manager = dbus.Interface(
                    self._bus.get_object("org.bluez", "/org/bluez"),
                    "org.bluez.ProfileManager1",
                )
                manager.UnregisterProfile(self._path)
            except dbus.DBusException as e:
                logger.warning("UnregisterProfile ch%d failed: %s",
                               self._channel, e)
            self._registered = False
        try:
            self.remove_from_connection()
        except Exception:
            pass
        self._clog("info", "listener.stop",
                   f"Stopped listening on channel {self._channel}",
                   channel=self._channel)

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="oha{sv}", out_signature="")
    def NewConnection(self, device_path, fd, fd_properties):
        peer = self._address_from_path(str(device_path))

        if hasattr(fd, "take"):
            fd_num = fd.take()
        else:
            fd_num = int(fd)

        try:
            sock = socket.fromfd(fd_num, AF_BLUETOOTH,
                                 socket.SOCK_STREAM, BTPROTO_RFCOMM)
        except OSError as e:
            logger.error("Cannot wrap fd from BlueZ for %s: %s", peer, e)
            self._clog("warn", "listener.fd_fail",
                       f"Cannot wrap fd for {peer}: {e}",
                       address=peer, channel=self._channel)
            try:
                os.close(fd_num)
            except OSError:
                pass
            return
        # socket.fromfd dup()s the fd; close the original.
        try:
            os.close(fd_num)
        except OSError:
            pass

        self._clog("info", "listener.accept",
                   f"Inbound SPP from {peer} on channel "
                   f"{self._channel}",
                   address=peer, channel=self._channel)

        try:
            self._dispatch(peer, sock, self._channel)
        except Exception:
            logger.exception("dispatch failed for %s", peer)
            try:
                sock.close()
            except OSError:
                pass

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="o", out_signature="")
    def RequestDisconnection(self, device_path):
        peer = self._address_from_path(str(device_path))
        self._clog("info", "listener.disconnect_req",
                   f"BlueZ requested disconnect for {peer} on channel "
                   f"{self._channel}",
                   address=peer, channel=self._channel)

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="", out_signature="")
    def Release(self):
        self._clog("info", "listener.release",
                   f"Profile on channel {self._channel} released by BlueZ",
                   channel=self._channel)

    @staticmethod
    def _address_from_path(device_path):
        tail = device_path.rsplit("/", 1)[-1]
        if tail.startswith("dev_"):
            return tail[4:].replace("_", ":").upper()
        return tail.upper()

    def _clog(self, level, step, detail, **kw):
        if self._conn_log is None:
            return
        getattr(self._conn_log, level)(step, detail, **kw)


class _DeviceLink:
    """One bidirectional RFCOMM link per enabled scanner.

    Can come up either way:

    * **Dial-out** — binds ``/dev/rfcomm<port>`` and opens it in a
      worker thread (Linux equivalent of Windows opening an outgoing
      COM port).  Works for scanners in SPP Slave mode.
    * **Listen-accept** — a shared :class:`_SPPListener` accepts an
      incoming connection and hands the socket back via
      :meth:`offer_inbound`.  Works for scanners in SPP Master / Cradle
      mode.

    Whichever arrives first wins; the other path backs off until the
    current link drops.
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
        # When the current link came in via the listener, we hold the
        # Python socket here to keep it alive for the duration of the
        # link (and to close it cleanly on drop).  None when the link
        # came in via dial-out / no link is up.
        self._inbound_sock = None
        # Queue of sockets offered by the shared _SPPListener.  _run
        # pops from this before attempting another dial.
        self._inbound_queue = queue.Queue()
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
        self._close_inbound_sock()
        self._drain_inbound_queue()
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
        resolved_channel = self._resolve_channel()
        if not resolved_channel:
            self._clog("warn", "device.dial.no_channel",
                       f"{self.address}: no SPP channel known yet, will "
                       "keep retrying / listening.  Switch the scanner "
                       "to SPP mode.",
                       address=self.address)

        while self._running:
            # Absorb an inbound connection that landed since we last
            # looked.  Non-blocking — we only want to short-circuit the
            # dial if one's already waiting.
            sock = self._pop_inbound_nowait()
            if sock is not None:
                self._serve(sock=sock, source="listen",
                            channel=resolved_channel or self._channel)
                continue

            # Respect the dial backoff, but keep an ear open for an
            # inbound connection during the wait — that's the whole
            # point of the hybrid.
            wait = self._next_attempt - time.monotonic()
            if wait > 0:
                sock = self._pop_inbound(timeout=min(wait, 2.0))
                if sock is not None:
                    self._serve(sock=sock, source="listen",
                                channel=resolved_channel or self._channel)
                continue

            channel = resolved_channel or self._channel
            fd, out_channel = self._attempt_dial(channel)
            if fd is None:
                continue

            self._serve(fd=fd, source="dial", channel=out_channel)

        # Clean exit.
        rfcomm_tty.release(self._port)

    # ── Inbound queue helpers ──────────────────────────────────────────

    def offer_inbound(self, sock):
        """Called from the listener thread when a scanner dials into us
        on this device's channel.  If we already have a live link we
        reject the duplicate; otherwise we enqueue the socket for
        :meth:`_run` to pick up."""
        with self._fd_lock:
            busy = self._fd is not None
        if busy or not self._running:
            try:
                sock.close()
            except OSError:
                pass
            return False
        self._inbound_queue.put(sock)
        return True

    def _pop_inbound_nowait(self):
        try:
            return self._inbound_queue.get_nowait()
        except queue.Empty:
            return None

    def _pop_inbound(self, timeout):
        try:
            return self._inbound_queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def _drain_inbound_queue(self):
        while True:
            s = self._pop_inbound_nowait()
            if s is None:
                return
            try:
                s.close()
            except OSError:
                pass

    # ── Dial path ──────────────────────────────────────────────────────

    def _attempt_dial(self, channel):
        """Run one dial-out attempt.  Returns ``(fd, channel)`` on
        success, ``(None, channel)`` on failure (with backoff already
        scheduled)."""
        if not channel:
            self._schedule_retry()
            return None, channel

        if not rfcomm_tty.bind(self._port, self.address, channel):
            self._clog("warn", "rfcomm.bind_fail",
                       f"rfcomm bind /dev/rfcomm{self._port} → "
                       f"{self.address} ch{channel} failed",
                       address=self.address, channel=channel)
            self._schedule_retry()
            return None, channel

        if not rfcomm_tty.tty_exists(self._port):
            self._clog("error", "rfcomm.tty_missing",
                       f"rfcomm bind succeeded but /dev/rfcomm"
                       f"{self._port} does not exist.  If running "
                       "under Docker, mount the host's /dev into the "
                       "container (add '- /dev:/dev' to volumes).",
                       address=self.address, channel=channel)
            self._schedule_retry()
            return None, channel

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
            self._register_failure(channel)
            return None, channel
        return fd, channel

    # ── Common serve path (works for TTY fd or inbound socket) ─────────

    def _serve(self, *, fd=None, sock=None, source, channel):
        """Take ownership of a freshly-established link and drive its
        read loop until it drops."""
        if sock is not None:
            fd = sock.fileno()
            self._inbound_sock = sock
            detail = (f"{self.address} accepted inbound SPP on channel "
                      f"{channel}")
        else:
            self._inbound_sock = None
            detail = (f"{self.address} connected on /dev/rfcomm"
                      f"{self._port} (channel {channel})")

        self._fail_count = 0
        self._next_attempt = 0.0
        with self._fd_lock:
            self._fd = fd

        self._register_with_router()
        self._clog("info", "device.connected", detail,
                   address=self.address, channel=channel)
        self._emit_connected()

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
                       f"({source}, channel {channel})",
                       address=self.address, channel=channel)
            # A queued-but-stale inbound is no good once the current
            # link drops — the scanner will redial if it wants.
            self._drain_inbound_queue()

        if self._running:
            time.sleep(1.0)

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

        Linux's RFCOMM TTY driver doesn't honour ``O_NONBLOCK`` on
        open — the open itself blocks until the kernel either completes
        the RFCOMM SABM handshake or gives up.  For a scanner that
        paired but never brought up SPP the kernel may never give up,
        so we run the open in a short-lived worker thread and bail out
        after :data:`DIAL_TIMEOUT`.  On timeout we release the RFCOMM
        binding, which causes the kernel to fail any pending open with
        ``ENODEV`` and lets the worker thread exit cleanly.
        """
        path = rfcomm_tty.device_path(self._port)
        result: dict = {}
        done = threading.Event()

        def _worker():
            try:
                result["fd"] = os.open(path, os.O_RDWR | os.O_NOCTTY)
            except OSError as e:
                result["error"] = e
            finally:
                done.set()

        threading.Thread(
            target=_worker,
            daemon=True,
            name=f"dial-open-{self.address}",
        ).start()

        if not done.wait(DIAL_TIMEOUT):
            self._clog("warn", "device.dial.timeout",
                       f"Dial to {path} ({self.address}) timed out after "
                       f"{DIAL_TIMEOUT:.0f}s; scanner may be asleep, out "
                       "of range, or not listening on this channel.",
                       address=self.address)
            # Unstick the kernel — releasing the binding causes the
            # pending open() to fail with ENODEV so the worker thread
            # finishes.  We'll rebind on the next dial attempt.
            rfcomm_tty.release(self._port)
            done.wait(2.0)
            if "fd" in result:
                self._safe_close(result["fd"])
            return None

        if "error" in result:
            self._clog("debug", "device.dial.fail",
                       f"open({path}) failed: {result['error']}",
                       address=self.address)
            return None

        fd = result["fd"]
        try:
            tty.setraw(fd)
        except (OSError, termios.error) as e:
            logger.warning("Configuring TTY on %s failed: %s", path, e)
            self._safe_close(fd)
            return None

        return fd

    @staticmethod
    def _safe_close(fd):
        try:
            os.close(fd)
        except OSError:
            pass

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
            sock = self._inbound_sock
            self._inbound_sock = None
        if sock is not None:
            # Closing the socket releases the same fd, so skip the
            # os.close() below for this case.
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            return
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def _close_inbound_sock(self):
        with self._fd_lock:
            sock = self._inbound_sock
            self._inbound_sock = None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
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
        # RFCOMM channel -> _SPPListener (shared across every link on
        # that channel; an SPP-Master scanner dials in and we route by
        # peer BT address).
        self._listeners = {}
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
        self._stop_all_listeners()
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

        # Line up listeners with whatever channels the current set of
        # links actually cares about.
        self._refresh_listeners(desired)

    # ── Inbound SPP listeners ──────────────────────────────────────────

    def _refresh_listeners(self, desired):
        """Start an :class:`_SPPListener` for every RFCOMM channel used by
        an enabled scanner, and tear down listeners for channels no
        longer in use."""
        wanted = {channel for _, channel in desired.values() if channel}

        with self._lock:
            current = set(self._listeners.keys())

        # Stop listeners no longer needed.
        for ch in current - wanted:
            self._stop_listener(ch)

        # Start listeners for new channels.
        for ch in wanted - current:
            self._start_listener(ch)

    def _start_listener(self, channel):
        listener = _SPPListener(
            channel=channel,
            dispatch=self._dispatch_incoming,
            bus=self._bt_manager.bus,
            conn_log=self._conn_log,
        )
        if not listener.start():
            return
        with self._lock:
            self._listeners[int(channel)] = listener

    def _stop_listener(self, channel):
        with self._lock:
            listener = self._listeners.pop(int(channel), None)
        if listener is not None:
            listener.stop()

    def _stop_all_listeners(self):
        with self._lock:
            channels = list(self._listeners.keys())
        for ch in channels:
            self._stop_listener(ch)

    def _dispatch_incoming(self, peer_addr, sock, channel):
        """Callback from :class:`_SPPListener`: route a freshly-accepted
        RFCOMM socket to the matching :class:`_DeviceLink`.  If no link
        owns this peer (unpaired / disabled device), we close the socket
        immediately."""
        peer = (peer_addr or "").upper()
        with self._lock:
            link = self._links.get(peer)
        if link is None:
            self._clog("warn", "listener.unknown_peer",
                       f"Inbound SPP from {peer} on channel {channel} "
                       "has no enabled paired device; dropping",
                       address=peer, channel=channel)
            try:
                sock.close()
            except OSError:
                pass
            return

        if not link.offer_inbound(sock):
            self._clog("debug", "listener.rejected",
                       f"{peer} already has a live link; rejecting "
                       f"duplicate inbound on channel {channel}",
                       address=peer, channel=channel)

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
