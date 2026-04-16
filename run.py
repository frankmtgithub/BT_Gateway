#!/usr/bin/env python3
"""BT Gateway — main entry point.

Initialises all components (config, BT manager, pairing agent, router,
PLC connection, device server, web UI) and runs the Flask/SocketIO web
server.
"""

import logging
import os
import signal
import sys

# Configure logging before importing application modules
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("bt_gateway")

from bt_gateway.config import Config
from bt_gateway.bt_manager import BluetoothManager
from bt_gateway.connection_log import ConnectionLog
from bt_gateway.message_router import MessageRouter
from bt_gateway.plc_connection import PLCConnection
from bt_gateway.device_server import DeviceServer
from bt_gateway.pairing_agent import register_agent, unregister_agent
from bt_gateway.web.app import create_app, socketio


def main():
    config_path = os.environ.get("CONFIG_PATH", "/data/config.json")
    logger.info("Starting BT Gateway (config: %s)", config_path)

    # ── Initialise components ────────────────────────────────────────────
    config = Config(config_path)

    # Shared connection log — instrumented by every piece of the
    # scanner/SPP path so the UI can show a full trace.
    conn_log = ConnectionLog(socketio=socketio)
    conn_log.info("gateway.boot", "BT Gateway starting")

    bt_manager = BluetoothManager(conn_log=conn_log)
    bt_manager.start()

    # Register the pairing agent so the gateway can accept PIN/passkey
    # requests automatically (both PLC pairing and device pairing).
    pairing_agent = register_agent(bt_manager.bus, conn_log=conn_log)

    router = MessageRouter(config, socketio)

    plc_conn = PLCConnection(config, router, bt_manager, socketio)
    router.set_plc_connection(plc_conn)

    device_server = DeviceServer(config, router, bt_manager, socketio,
                                 conn_log=conn_log)

    # ── Power on adapters ────────────────────────────────────────────────
    plc_adapter = config.get("plc_adapter")
    device_adapter = config.get("device_adapter")

    if plc_adapter:
        bt_manager.power_adapter(plc_adapter, True)
        bt_manager.set_pairable(plc_adapter, True)
        logger.info("PLC adapter %s powered on", plc_adapter)

    if device_adapter:
        bt_manager.power_adapter(device_adapter, True)
        logger.info("Device adapter %s powered on", device_adapter)

    # ── Start services ───────────────────────────────────────────────────
    plc_conn.start()
    device_server.start()

    # ── Create Flask app ─────────────────────────────────────────────────
    app = create_app(config, bt_manager, router, plc_conn, device_server,
                     conn_log=conn_log)

    # ── Graceful shutdown ────────────────────────────────────────────────
    def shutdown(sig, frame):
        logger.info("Shutting down (signal %s)...", sig)
        plc_conn.stop()
        device_server.stop()
        unregister_agent(bt_manager.bus)
        bt_manager.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # ── Run web server ───────────────────────────────────────────────────
    host = config.get("web_host", "0.0.0.0")
    port = config.get("web_port", 8080)
    logger.info("Web interface at http://%s:%d", host, port)

    socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)


if __name__ == "__main__":
    main()
