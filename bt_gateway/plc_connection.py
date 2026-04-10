"""PLC Bluetooth SPP client connection with automatic reconnection.

The Pi initiates the RFCOMM connection to the PLC and maintains it
indefinitely, retrying forever if the connection drops or fails.
Messages from the PLC are newline-delimited JSON.
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

RECV_BUFFER = 4096


class PLCConnection:
    """Manages the outgoing SPP connection to the PLC."""

    def __init__(self, config, router, socketio=None):
        self._config = config
        self._router = router
        self._socketio = socketio
        self._sock = None
        self._status = "disconnected"
        self._running = False
        self._thread = None
        self._send_lock = threading.Lock()

    @property
    def status(self):
        return self._status

    @property
    def is_connected(self):
        return self._status == "connected"

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

    def _run(self):
        """Main loop: connect → read → reconnect, forever."""
        while self._running:
            plc_addr = self._config.get("plc_address", "")
            plc_channel = self._config.get("plc_channel", 1)
            plc_adapter = self._config.get("plc_adapter", "")
            reconnect_interval = self._config.get("plc_reconnect_interval", 5)

            if not plc_addr:
                logger.warning("No PLC address configured, waiting...")
                self._set_status("not_configured")
                self._sleep(reconnect_interval)
                continue

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

    def _connect(self, address, channel, adapter_name):
        """Attempt to connect to the PLC over RFCOMM."""
        self._set_status("connecting")
        logger.info("Connecting to PLC at %s channel %d...", address, channel)

        try:
            sock = socket.socket(AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)

            # Bind to the specific PLC adapter if configured
            if adapter_name:
                from bt_gateway.bt_manager import BluetoothManager
                # We need a temporary manager to look up the address, but in
                # practice this is called from main where the manager exists.
                # Use a simpler approach: read adapter address from config or
                # resolve at start.  For now, we resolve it here.
                adapter_addr = self._resolve_adapter_address(adapter_name)
                if adapter_addr:
                    sock.bind((adapter_addr, 0))
                    logger.info("Bound PLC socket to adapter %s (%s)", adapter_name, adapter_addr)

            sock.settimeout(10)
            sock.connect((address, channel))
            sock.settimeout(None)
            self._sock = sock
            self._set_status("connected")
            logger.info("Connected to PLC at %s", address)
            return True
        except OSError as e:
            logger.error("Failed to connect to PLC: %s", e)
            self._close_socket()
            self._set_status("disconnected")
            return False

    def _resolve_adapter_address(self, adapter_name):
        """Resolve an adapter name (e.g. hci0) to its BT address."""
        try:
            # Read from /sys/class/bluetooth/<adapter>/address
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
                buffer += data.decode("utf-8", errors="replace")
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
                self._socketio.emit("plc_status", {"status": status}, namespace="/")

    def _sleep(self, seconds):
        """Interruptible sleep."""
        deadline = time.monotonic() + seconds
        while self._running and time.monotonic() < deadline:
            time.sleep(0.5)
