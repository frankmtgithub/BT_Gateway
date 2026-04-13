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
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"
A2DP_SINK_UUID = "0000110b-0000-1000-8000-00805f9b34fb"
A2DP_SOURCE_UUID = "0000110a-0000-1000-8000-00805f9b34fb"
HFP_HF_UUID = "0000111e-0000-1000-8000-00805f9b34fb"
HSP_HS_UUID = "00001108-0000-1000-8000-00805f9b34fb"

# Non-SPP profiles we actively disconnect to stop BlueZ classifying the
# PLC link as an audio / HID connection.
NON_SPP_UUIDS = [HID_UUID, A2DP_SINK_UUID, A2DP_SOURCE_UUID,
                 HFP_HF_UUID, HSP_HS_UUID]

RECV_BUFFER = 4096


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
            plc_channel = self._config.get("plc_channel", 1)
            plc_adapter = self._config.get("plc_adapter", "")
            reconnect_interval = self._config.get("plc_reconnect_interval", 5)

            if not plc_adapter:
                self._current_address = ""
                self._set_status("not_configured")
                self._sleep(reconnect_interval)
                continue

            # The PLC address is whatever device is paired on the PLC
            # adapter.  Exactly one device is expected; if none is paired,
            # wait and retry.
            paired = self._bt_manager.get_single_paired_device(plc_adapter)
            if not paired:
                self._current_address = ""
                self._set_status("not_paired")
                self._sleep(reconnect_interval)
                continue

            plc_addr = paired["address"]
            self._current_address = plc_addr

            # Make sure the PLC is trusted and the link is marked as SPP
            # (not audio/HID) before we open the RFCOMM data socket.
            self._prepare_plc_link(plc_addr, plc_adapter)

            # Attempt connection
            if not self._connect(plc_addr, plc_channel, plc_adapter):
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
        """Ensure the PLC is trusted and that any non-SPP profiles that
        BlueZ may have auto-connected (audio, HID) are disconnected.

        Then explicitly request that BlueZ connect the SPP profile so the
        link is tracked as Serial Port, not Audio.  This is a best-effort
        call — if BlueZ has already opened the channel it will succeed
        silently.
        """
        self._bt_manager.set_device_trusted(address, True, adapter_name)

        # Kill any audio/HID profile that BlueZ brought up automatically.
        for uuid in NON_SPP_UUIDS:
            self._bt_manager.disconnect_profile(address, uuid, adapter_name)

        # Ask BlueZ to bring up SPP specifically.
        self._bt_manager.connect_profile(address, SPP_UUID, adapter_name)

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
            sock.settimeout(None)
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
        """Read newline-delimited JSON messages from the PLC."""
        buffer = ""
        while self._running and self._sock:
            try:
                data = self._sock.recv(RECV_BUFFER)
                if not data:
                    logger.info("PLC connection closed by remote")
                    break
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
                continue
            except OSError as e:
                logger.error("PLC read error: %s", e)
                break

    def _close_socket(self):
        if self._sock:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

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
