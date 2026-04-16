"""PLC Bluetooth SPP client connection with automatic reconnection.

The Pi initiates the RFCOMM connection to the PLC and maintains it
indefinitely, retrying forever if the connection drops or fails.
Messages from the PLC are newline-delimited JSON.

The PLC's MAC address is NOT configured manually.  It is discovered from
whatever single paired device is present on the PLC adapter.
"""

import logging
import os
import socket
import threading
import time

logger = logging.getLogger(__name__)

try:
    from socket import AF_BLUETOOTH, BTPROTO_RFCOMM
except ImportError:
    AF_BLUETOOTH = 31
    BTPROTO_RFCOMM = 3

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"

# The PLC side is always Serial Port Profile — only scanners / remote
# devices ever need the HID→SPP handover.  Any other profile on the PLC
# address is therefore unexpected, and we don't try to disconnect HID /
# audio defensively because doing so used to spam BlueZ with UUIDs the
# PLC never advertised.

RECV_BUFFER = 4096

# While connected, recv() wakes up every RECV_TIMEOUT seconds so we can
# probe the socket for a half-open / dead link.  If no data has arrived in
# PROBE_INTERVAL seconds, we do a non-blocking MSG_PEEK; if the remote has
# torn the RFCOMM channel down (e.g. Windows closed the virtual COM port)
# the peek returns EOF or errors, and we drop out of the read loop.
RECV_TIMEOUT = 3
PROBE_INTERVAL = 5


class PLCConnection:
    """Manages the outgoing SPP connection to the PLC."""

    def __init__(self, config, router, bt_manager, socketio=None):
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._socketio = socketio
        self._sock = None
        self._status = "disconnected"
        self._current_address = ""
        self._current_channel = 0
        self._running = False
        self._thread = None
        self._send_lock = threading.Lock()

    @property
    def status(self):
        return self._status

    @property
    def is_connected(self):
        return self._status == "connected"

    @property
    def address(self):
        return self._current_address

    @property
    def channel(self):
        """The RFCOMM channel used for the active (or last) connection."""
        return self._current_channel

    def start(self):
        """Start the PLC connection manager thread."""
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="plc-connection"
        )
        self._thread.start()
        logger.info("PLC connection manager started")

    def stop(self):
        self._running = False
        self._close_socket()
        if self._thread:
            self._thread.join(timeout=5)
        logger.info("PLC connection manager stopped")

    def send(self, data):
        """Send a message to the PLC (newline-terminated)."""
        with self._send_lock:
            if self._sock and self._status == "connected":
                try:
                    payload = data if isinstance(data, str) else str(data)
                    self._sock.sendall((payload + "\n").encode("utf-8"))
                    return True
                except OSError as e:
                    logger.error("Send to PLC failed: %s", e)
                    self._set_status("disconnected")
                    return False
            return False

    # ── Main loop ──────────────────────────────────────────────────────

    def _run(self):
        """Main loop: connect → read → reconnect, forever."""
        while self._running:
            plc_adapter = self._config.get("plc_adapter", "")
            reconnect_interval = self._config.get("plc_reconnect_interval", 5)
            channel_override = int(self._config.get("plc_channel", 0) or 0)

            if not plc_adapter:
                self._current_address = ""
                self._current_channel = 0
                self._set_status("not_configured")
                self._sleep(reconnect_interval)
                continue

            # The PLC address is whatever device is paired on the PLC
            # adapter.  Exactly one device is expected; if none is paired,
            # wait and retry.
            paired = self._bt_manager.get_single_paired_device(plc_adapter)
            if not paired:
                self._current_address = ""
                self._current_channel = 0
                self._set_status("not_paired")
                self._sleep(reconnect_interval)
                continue

            plc_addr = paired["address"]
            self._current_address = plc_addr

            # Make sure the PLC is trusted and the link is marked as SPP
            # (not audio/HID) before we open the RFCOMM data socket.
            self._prepare_plc_link(plc_addr, plc_adapter)

            # Resolve the RFCOMM channel.  Preferred source is SDP: the PLC
            # advertises its Serial Port service on whatever channel Windows
            # (or the PLC firmware) picked for the user's COM port, so the
            # gateway doesn't need the user to know or type it.  If SDP
            # discovery fails, fall back to the manual override, and finally
            # channel 1 as a last resort.
            channel = self._bt_manager.sdp_find_spp_channel(plc_addr)
            if channel is not None:
                logger.info("SDP: SPP on %s uses RFCOMM channel %d",
                            plc_addr, channel)
            elif channel_override > 0:
                channel = channel_override
                logger.warning("SDP discovery failed on %s, using configured "
                               "override channel %d", plc_addr, channel)
            else:
                channel = 1
                logger.warning("SDP discovery failed on %s and no override "
                               "configured, trying channel 1", plc_addr)
            self._current_channel = channel

            # Attempt connection
            if not self._connect(plc_addr, channel, plc_adapter):
                self._sleep(reconnect_interval)
                continue

            # Read loop
            self._read_loop()

            # Connection lost — will retry
            self._close_socket()
            self._set_status("disconnected")
            logger.info("PLC connection lost, reconnecting in %ds...", reconnect_interval)
            self._sleep(reconnect_interval)

    # ── Connect helpers ────────────────────────────────────────────────

    def _prepare_plc_link(self, address, adapter_name):
        """Mark the PLC as trusted and ask BlueZ to bring up the SPP
        profile.  The PLC is always SPP — HID/audio disconnect logic
        belongs on the devices side, not here.

        ``silent=True`` on the profile calls keeps the connection-log
        (which is a user-facing UI panel focused on scanner traffic)
        free of the 5-second PLC reconnect churn.  Python's stdlib
        logger still records everything for diagnostics.
        """
        self._bt_manager.set_device_trusted(address, True, adapter_name)
        # Ask BlueZ to bring up SPP specifically.  Best-effort: if the
        # channel is already open, this succeeds silently.
        self._bt_manager.connect_profile(address, SPP_UUID, adapter_name,
                                         silent=True)

    def _connect(self, address, channel, adapter_name):
        """Attempt to connect to the PLC over RFCOMM."""
        self._set_status("connecting")
        logger.info("Connecting to PLC at %s channel %d...", address, channel)

        try:
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)

            # Bind to the specific PLC adapter if configured
            if adapter_name:
                adapter_addr = self._resolve_adapter_address(adapter_name)
                if adapter_addr:
                    sock.bind((adapter_addr, 0))
                    logger.info("Bound PLC socket to adapter %s (%s)",
                                adapter_name, adapter_addr)

            sock.settimeout(10)
            sock.connect((address, channel))
            # Short timeout while connected — the read loop uses it to
            # periodically probe the link so we notice when the remote
            # tears the channel down (e.g. Hercules closed the COM port).
            sock.settimeout(RECV_TIMEOUT)
            self._sock = sock
            self._set_status("connected")
            logger.info("Connected to PLC at %s over SPP", address)
            return True
        except OSError as e:
            logger.error("Failed to connect to PLC: %s", e)
            self._close_socket()
            self._set_status("disconnected")
            return False

    def _resolve_adapter_address(self, adapter_name):
        """Resolve an adapter name (e.g. hci0) to its BT address."""
        # Prefer BlueZ's canonical answer when possible.
        try:
            addr = self._bt_manager.get_adapter_address(adapter_name)
            if addr:
                return addr.upper()
        except Exception:
            pass
        try:
            path = f"/sys/class/bluetooth/{adapter_name}/address"
            if os.path.exists(path):
                with open(path) as f:
                    return f.read().strip().upper()
        except OSError:
            pass
        logger.warning("Could not resolve address for adapter %s", adapter_name)
        return None

    def _read_loop(self):
        """Read newline-delimited JSON messages from the PLC.

        Uses a short recv timeout so the loop can periodically probe the
        socket while idle — that way when the PLC side tears the RFCOMM
        channel down (for example, Hercules closing the COM port on
        Windows), we actually notice and mark the connection disconnected,
        instead of blocking forever in recv().

        Also drives the configurable keep-alive: when enabled, every
        ``plc_keepalive_interval`` seconds we write
        ``plc_keepalive_message + "\\n"`` to the PLC so the remote end
        sees that we're still there even during long idle periods.
        """
        buffer = ""
        last_rx = time.monotonic()
        last_keepalive = time.monotonic()
        while self._running and self._sock:
            try:
                data = self._sock.recv(RECV_BUFFER)
                if not data:
                    logger.info("PLC connection closed by remote (EOF)")
                    break
                last_rx = time.monotonic()
                decoded = data.decode("utf-8", errors="replace")
                buffer += decoded
                # Let the router see the raw bytes (debug mode uses this)
                self._router.notify_plc_raw(decoded)
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if line:
                        self._router.route_from_plc(line)
            except socket.timeout:
                # Idle — probe the link so we detect half-open states.
                if time.monotonic() - last_rx >= PROBE_INTERVAL:
                    if not self._probe_link_alive():
                        logger.info(
                            "PLC link probe failed — remote closed "
                            "RFCOMM channel, marking disconnected"
                        )
                        break
                    last_rx = time.monotonic()
                # Fire the configurable keep-alive if it's due.  Pulling
                # the settings each tick means edits from the Settings UI
                # take effect without reconnecting.
                if self._maybe_send_keepalive(last_keepalive):
                    last_keepalive = time.monotonic()
                continue
            except OSError as e:
                logger.error("PLC read error: %s", e)
                break

    def _maybe_send_keepalive(self, last_keepalive):
        """Send the configured keep-alive if it's due.

        Returns True if a keep-alive was sent (caller should reset its
        ``last_keepalive`` timer).  Returns False otherwise — including
        when the feature is disabled or misconfigured.
        """
        enabled = bool(self._config.get("plc_keepalive_enabled", False))
        if not enabled:
            return False
        try:
            interval = int(self._config.get("plc_keepalive_interval", 0) or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            return False
        message = self._config.get("plc_keepalive_message", "") or ""
        if not message:
            return False
        if time.monotonic() - last_keepalive < interval:
            return False
        try:
            self.send(message)
        except Exception:
            logger.exception("PLC keep-alive send failed")
            return False
        return True

    def _probe_link_alive(self):
        """Check whether the PLC socket is still usable.

        Three-step probe:

        1. MSG_PEEK + MSG_DONTWAIT: catches the clean case where the remote
           sent a DISC and the kernel has queued EOF.
        2. SO_ERROR: picks up any pending socket error (ECONNRESET, EPIPE)
           that the kernel has noted but not yet reported via recv/send.
        3. Active single-byte heartbeat write ("\\n"): catches the half-open
           case where the remote process stopped listening (e.g. Hercules
           closed COM6 on Windows) without tearing down RFCOMM.  An empty
           line between messages is a no-op for the newline-delimited JSON
           protocol; on a dead link the send raises EPIPE / ENOTCONN.

        Returns True if the socket still looks usable, False otherwise.
        """
        if not self._sock:
            return False

        # (1) Passive EOF check.
        try:
            peek = self._sock.recv(
                1, socket.MSG_PEEK | socket.MSG_DONTWAIT
            )
            if not peek:
                return False
        except BlockingIOError:
            pass  # No data available — keep probing.
        except OSError:
            return False

        # (2) Kernel-reported socket error?
        try:
            err = self._sock.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
            if err:
                return False
        except OSError:
            return False

        # (3) Active write heartbeat.  Must go through the send lock so it
        # doesn't interleave with a concurrent send().
        try:
            with self._send_lock:
                self._sock.sendall(b"\n")
        except OSError:
            return False
        return True

    def _close_socket(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # Also tear down BlueZ's managed SPP profile so the next connect
        # attempt starts from a clean state instead of reusing a stale
        # ConnectProfile() link that BlueZ thinks is still up.
        try:
            addr = self._current_address
            adapter = self._config.get("plc_adapter", "")
            if addr:
                self._bt_manager.disconnect_profile(addr, SPP_UUID, adapter,
                                                    silent=True)
        except Exception:
            pass

    def _set_status(self, status):
        old = self._status
        self._status = status
        if old != status:
            logger.info("PLC status: %s → %s", old, status)
            if self._socketio:
                self._socketio.emit("plc_status", {
                    "status": status,
                    "address": self._current_address,
                }, namespace="/")

    def _sleep(self, seconds):
        """Interruptible sleep."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)
