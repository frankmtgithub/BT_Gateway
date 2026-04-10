"""Web routes and API endpoints for BT Gateway."""

import logging

from flask import Blueprint, current_app, jsonify, render_template, request

logger = logging.getLogger(__name__)

bp = Blueprint("gateway", __name__)


# ── Page routes ──────────────────────────────────────────────────────────────

@bp.route("/")
def dashboard():
    return render_template("dashboard.html")


@bp.route("/pairing")
def pairing():
    return render_template("pairing.html")


@bp.route("/settings")
def settings():
    return render_template("settings.html")


# ── Status API ───────────────────────────────────────────────────────────────

@bp.route("/api/status")
def api_status():
    return jsonify(current_app.router.get_status())


# ── Adapter API ──────────────────────────────────────────────────────────────

@bp.route("/api/adapters")
def api_adapters():
    adapters = current_app.bt_manager.list_adapters()
    return jsonify(adapters)


# ── Pairing API ──────────────────────────────────────────────────────────────

@bp.route("/api/pairing/mode", methods=["GET"])
def api_pairing_mode_get():
    return jsonify({"enabled": current_app.device_server.pairing_mode})


@bp.route("/api/pairing/enable", methods=["POST"])
def api_pairing_enable():
    adapter_name = current_app.gateway_config.get("device_adapter", "")
    if not adapter_name:
        return jsonify({"error": "No device adapter configured"}), 400

    # Start discovery and make discoverable
    current_app.device_server.set_pairing_mode(True)
    current_app.bt_manager.start_discovery(adapter_name)
    return jsonify({"status": "pairing_enabled"})


@bp.route("/api/pairing/disable", methods=["POST"])
def api_pairing_disable():
    adapter_name = current_app.gateway_config.get("device_adapter", "")
    if adapter_name:
        current_app.bt_manager.stop_discovery(adapter_name)
    current_app.device_server.set_pairing_mode(False)
    return jsonify({"status": "pairing_disabled"})


@bp.route("/api/pairing/devices")
def api_pairing_devices():
    """List discovered and paired devices on the device adapter."""
    adapter_name = current_app.gateway_config.get("device_adapter", "")
    if not adapter_name:
        return jsonify([])
    devices = current_app.bt_manager.list_devices(adapter_name)
    return jsonify(devices)


@bp.route("/api/pairing/pair", methods=["POST"])
def api_pair_device():
    data = request.get_json()
    address = data.get("address", "")
    if not address:
        return jsonify({"error": "No address provided"}), 400

    adapter_name = current_app.gateway_config.get("device_adapter", "")
    success = current_app.bt_manager.pair_device(address, adapter_name)
    if success:
        name = current_app.gateway_config.add_device(address)
        return jsonify({"status": "paired", "name": name})
    return jsonify({"error": "Pairing failed"}), 500


@bp.route("/api/pairing/remove", methods=["POST"])
def api_remove_device():
    data = request.get_json()
    address = data.get("address", "")
    if not address:
        return jsonify({"error": "No address provided"}), 400

    adapter_name = current_app.gateway_config.get("device_adapter", "")

    # Disconnect if active
    profile = current_app.device_server.profile
    if profile:
        profile._disconnect_device(address)

    # Remove from BlueZ
    current_app.bt_manager.remove_device(address, adapter_name)
    # Remove from config
    current_app.gateway_config.remove_device(address)
    return jsonify({"status": "removed"})


@bp.route("/api/pairing/remove-all", methods=["POST"])
def api_remove_all():
    """Disconnect and unpair all devices."""
    adapter_name = current_app.gateway_config.get("device_adapter", "")
    profile = current_app.device_server.profile

    # Disconnect all active connections
    if profile:
        profile.disconnect_all()

    # Remove all from BlueZ
    devices = current_app.gateway_config.get_devices()
    for address in devices:
        current_app.bt_manager.remove_device(address, adapter_name)

    # Clear config
    current_app.gateway_config.remove_all_devices()
    return jsonify({"status": "all_removed"})


# ── Settings API ─────────────────────────────────────────────────────────────

@bp.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = current_app.gateway_config.data
    return jsonify({
        "plc_adapter": cfg.get("plc_adapter", ""),
        "device_adapter": cfg.get("device_adapter", ""),
        "plc_address": cfg.get("plc_address", ""),
        "plc_channel": cfg.get("plc_channel", 1),
        "plc_reconnect_interval": cfg.get("plc_reconnect_interval", 5),
        "web_port": cfg.get("web_port", 8080),
    })


@bp.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json()
    cfg = current_app.gateway_config

    allowed = [
        "plc_adapter", "device_adapter", "plc_address",
        "plc_channel", "plc_reconnect_interval",
    ]
    for key in allowed:
        if key in data:
            value = data[key]
            if key == "plc_channel":
                value = int(value)
            elif key == "plc_reconnect_interval":
                value = int(value)
            cfg.set(key, value)

    return jsonify({"status": "saved"})


# ── Device renaming API ─────────────────────────────────────────────────────

@bp.route("/api/devices/<address>/rename", methods=["POST"])
def api_rename_device(address):
    # URL uses dashes instead of colons for the address
    address = address.replace("-", ":").upper()
    data = request.get_json()
    new_name = data.get("name", "")
    if not new_name:
        return jsonify({"error": "No name provided"}), 400

    success = current_app.gateway_config.rename_device(address, new_name)
    if success:
        return jsonify({"status": "renamed", "name": new_name})
    return jsonify({"error": "Device not found"}), 404
