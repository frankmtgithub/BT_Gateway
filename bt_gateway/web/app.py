"""Flask application factory for BT Gateway web interface."""

import logging

from flask import Flask
from flask_socketio import SocketIO

logger = logging.getLogger(__name__)

socketio = SocketIO()


def create_app(config, bt_manager, router, plc_connection, device_server):
    """Create and configure the Flask application.

    All gateway components are stored on the app object so routes can
    access them via ``current_app``.
    """
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config["SECRET_KEY"] = "bt-gateway-local"

    # Store references for use in routes
    app.gateway_config = config
    app.bt_manager = bt_manager
    app.router = router
    app.plc_connection = plc_connection
    app.device_server = device_server

    # Register routes
    from bt_gateway.web.routes import bp
    app.register_blueprint(bp)

    socketio.init_app(app, async_mode="threading", cors_allowed_origins="*")

    @socketio.on("connect")
    def on_connect():
        socketio.emit("status_update", router.get_status())

    return app
