"""Device SPP server using BlueZ Profile1 D-Bus API.

Registers an SPP (Serial Port Profile) service on the device adapter.
When a remote device connects, BlueZ hands us the RFCOMM file descriptor
through the Profile1.NewConnection callback.  A reader thread is spawned
for each connected device to forward data to the message router.
"""

import logging
import os
import socket
import threading

import dbus
import dbus.service

logger = logging.getLogger(__name__)

try:
    from socket import AF_BLUETOOTH, BTPROTO_RFCOMM
except ImportError:
    AF_BLUETOOTH = 31
    BTPROTO_RFCOMM = 3

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
PROFILE_PATH = "/org/bluez/btgateway/spp_profile"
RECV_BUFFER = 4096


class DeviceConnection:
    """Wraps a single RFCOMM connection to a remote device."""

    def __init__(self, address, sock, on_data, on_disconnect):
        self.address = address
        self._sock = sock
        self._on_data = on_data
        self._on_disconnect = on_disconnect
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

    BlueZ calls NewConnection when a remote device opens an RFCOMM channel
    to our registered SPP service.
    """

    def __init__(self, bus, config, router, bt_manager, socketio=None):
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._socketio = socketio
        self._connections = {}  # address -> DeviceConnection
        self._lock = threading.Lock()
        super().__init__(bus, PROFILE_PATH)

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

        logger.info("New device connection from %s (path: %s)", address, device_path)

        # Take ownership of the file descriptor
        if hasattr(fd, "take"):
            fd_num = fd.take()
        else:
            fd_num = int(fd)

        # Create a socket from the file descriptor
        try:
            sock = socket.fromfd(fd_num, AF_BLUETOOTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
            os.close(fd_num)  # fromfd dups the fd
        except OSError as e:
            logger.error("Failed to create socket from fd for %s: %s", address, e)
            os.close(fd_num)
            return

        # Register device in config if not already present
        device_name = self._config.add_device(address)

        # Create and start the device connection handler
        conn = DeviceConnection(
            address=address,
            sock=sock,
            on_data=self._on_device_data,
            on_disconnect=self._on_device_disconnect,
        )

        with self._lock:
            # Close existing connection from same device if any
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

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="o", out_signature="")
    def RequestDisconnection(self, device_path):
        addr_part = device_path.split("/")[-1]
        if addr_part.startswith("dev_"):
            address = addr_part[4:].replace("_", ":").upper()
        else:
            address = addr_part
        logger.info("Disconnection requested for %s", address)
        self._disconnect_device(address)

    @dbus.service.method("org.bluez.Profile1",
                         in_signature="", out_signature="")
    def Release(self):
        logger.info("SPP Profile released by BlueZ")

    def _on_device_data(self, address, data):
        """Called by a DeviceConnection reader thread when data arrives."""
        self._router.route_from_device(address, data)

    def _on_device_disconnect(self, address):
        """Called when a device connection drops."""
        self._disconnect_device(address)

    def _disconnect_device(self, address):
        with self._lock:
            conn = self._connections.pop(address, None)
        if conn:
            conn.stop()
        self._router.unregister_device(address)
        device_name = self._config.get_device_name(address) or address
        logger.info("Device %s (%s) disconnected", address, device_name)
        if self._socketio:
            self._socketio.emit("device_disconnected", {
                "address": address,
                "name": device_name,
            }, namespace="/")

    def disconnect_all(self):
        """Disconnect all active device connections."""
        with self._lock:
            addresses = list(self._connections.keys())
        for addr in addresses:
            self._disconnect_device(addr)

    def get_active_connections(self):
        with self._lock:
            return list(self._connections.keys())


class DeviceServer:
    """Manages the SPP server lifecycle: profile registration, pairing mode."""

    def __init__(self, config, router, bt_manager, socketio=None):
        self._config = config
        self._router = router
        self._bt_manager = bt_manager
        self._socketio = socketio
        self._profile = None
        self._pairing_mode = False

    def start(self):
        """Register the SPP profile with BlueZ and power on the device adapter."""
        adapter_name = self._config.get("device_adapter", "")
        if not adapter_name:
            logger.warning("No device adapter configured")
            return False

        # Power on the adapter
        self._bt_manager.power_adapter(adapter_name, True)

        # Register SPP Profile
        self._profile = SPPProfile(
            self._bt_manager.bus,
            self._config,
            self._router,
            self._bt_manager,
            self._socketio,
        )

        try:
            manager = dbus.Interface(
                self._bt_manager.bus.get_object("org.bluez", "/org/bluez"),
                "org.bluez.ProfileManager1",
            )
            opts = {
                "Name": dbus.String("BT Gateway SPP"),
                "Role": dbus.String("server"),
                "Channel": dbus.UInt16(1),
                "AutoConnect": dbus.Boolean(False),
                "RequireAuthentication": dbus.Boolean(False),
                "RequireAuthorization": dbus.Boolean(False),
            }
            manager.RegisterProfile(PROFILE_PATH, SPP_UUID, opts)
            logger.info("SPP Profile registered on %s", adapter_name)
            return True
        except dbus.DBusException as e:
            logger.error("Failed to register SPP profile: %s", e)
            return False

    def stop(self):
        if self._profile:
            self._profile.disconnect_all()
        self.set_pairing_mode(False)

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
    def profile(self):
        return self._profile
