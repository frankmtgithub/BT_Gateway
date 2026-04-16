"""Web routes and API endpoints for BT Gateway."""

import datetime
import logging
import os
import sys
import threading

from flask import (
    Blueprint, Response, current_app, jsonify, render_template, request,
)

logger = logging.getLogger(__name__)

bp = Blueprint("gateway", __name__)

SPP_UUID = "00001101-0000-1000-8000-00805f9b34fb"
HID_UUID = "00001124-0000-1000-8000-00805f9b34fb"


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


@bp.route("/wizard")
def wizard():
    return render_template("wizard.html")


# ── Status API ───────────────────────────────────────────────────────────────

@bp.route("/api/status")
def api_status():
    return jsonify(current_app.router.get_status())


# ── Adapter API ──────────────────────────────────────────────────────────────

@bp.route("/api/adapters")
def api_adapters():
    adapters = current_app.bt_manager.list_adapters()
    return jsonify(adapters)


@bp.route("/api/adapters/<adapter_name>/power", methods=["POST"])
def api_adapter_power(adapter_name):
    """Power an adapter on or off.

    Body: {"powered": true/false}
    """
    data = request.get_json(silent=True) or {}
    powered = bool(data.get("powered", True))
    success = current_app.bt_manager.power_adapter(adapter_name, powered)
    if not success:
        return jsonify({"error": f"Failed to power {adapter_name}"}), 500
    return jsonify({
        "status": "ok",
        "adapter": adapter_name,
        "powered": powered,
    })


def _resolve_pairing_adapter(data=None):
    """Get the adapter to use for pairing, preferring the request payload
    but falling back to the configured device adapter."""
    if data:
        adapter = (data.get("adapter") or "").strip()
        if adapter:
            return adapter
    return current_app.gateway_config.get("device_adapter", "")


def _plc_paired_address():
    """Return the uppercase MAC of the device currently paired on the PLC
    adapter, or '' if none / not configured.  Used to keep that device
    from being re-paired on the devices adapter."""
    plc_adapter = current_app.gateway_config.get("plc_adapter", "")
    if not plc_adapter:
        return ""
    try:
        paired = current_app.bt_manager.get_single_paired_device(plc_adapter)
    except Exception:
        return ""
    if not paired:
        return ""
    return (paired.get("address") or "").upper()


# ── Pairing API (device adapter) ─────────────────────────────────────────────

@bp.route("/api/pairing/mode", methods=["GET"])
def api_pairing_mode_get():
    return jsonify({"enabled": current_app.device_server.pairing_mode})


@bp.route("/api/pairing/enable", methods=["POST"])
def api_pairing_enable():
    data = request.get_json(silent=True) or {}
    adapter_name = _resolve_pairing_adapter(data)
    if not adapter_name:
        return jsonify({"error": "No adapter selected"}), 400

    # Make sure the adapter is powered before discovery
    current_app.bt_manager.power_adapter(adapter_name, True)

    # Make discoverable/pairable on the chosen adapter and start discovery
    current_app.device_server.set_pairing_mode(True, adapter_name=adapter_name)
    current_app.bt_manager.start_discovery(adapter_name)
    return jsonify({"status": "pairing_enabled", "adapter": adapter_name})


@bp.route("/api/pairing/disable", methods=["POST"])
def api_pairing_disable():
    data = request.get_json(silent=True) or {}
    adapter_name = _resolve_pairing_adapter(data)
    if adapter_name:
        current_app.bt_manager.stop_discovery(adapter_name)
    current_app.device_server.set_pairing_mode(False, adapter_name=adapter_name)
    return jsonify({"status": "pairing_disabled", "adapter": adapter_name})


@bp.route("/api/pairing/devices")
def api_pairing_devices():
    """List discovered and paired devices on the requested adapter.

    Each entry is annotated with ``plc_paired: true`` if that address is
    the device currently paired on the PLC adapter — the UI uses this to
    hide the Pair button and show a "PLC, not usable here" badge so the
    user can't accidentally bind the same physical device to both sides.
    """
    adapter_name = (request.args.get("adapter") or "").strip() \
        or current_app.gateway_config.get("device_adapter", "")
    if not adapter_name:
        return jsonify([])
    devices = current_app.bt_manager.list_devices(adapter_name)
    plc_addr = _plc_paired_address()
    for dev in devices:
        dev["plc_paired"] = bool(plc_addr) and (
            dev.get("address", "").upper() == plc_addr
        )
    return jsonify(devices)


@bp.route("/api/pairing/pair", methods=["POST"])
def api_pair_device():
    data = request.get_json() or {}
    address = data.get("address", "")
    if not address:
        return jsonify({"error": "No address provided"}), 400

    # Block: this device is already bound to the PLC adapter.  Pairing it
    # here too would leave BlueZ with split-ownership confusion on the same
    # physical device; force the user to unpair it from the PLC first.
    if address.upper() == _plc_paired_address():
        return jsonify({
            "error": "This device is paired on the PLC adapter. "
                     "Unpair it from the PLC side first if you want to use "
                     "it as a device."
        }), 409

    adapter_name = _resolve_pairing_adapter(data)
    success = current_app.bt_manager.pair_device(address, adapter_name)
    if success:
        # Force SPP, kill HID so barcode scanners don't act as keyboards
        current_app.bt_manager.disconnect_profile(address, HID_UUID, adapter_name)
        current_app.bt_manager.connect_profile(address, SPP_UUID, adapter_name)
        entry = current_app.gateway_config.add_device(address)
        name = entry["name"] if isinstance(entry, dict) else entry
        port = entry["port"] if isinstance(entry, dict) else None
        # Bring up the SPP listener now so the scanner can initiate its
        # side of the connection as soon as it switches to SPP mode.
        current_app.device_server.refresh_profiles()
        return jsonify({
            "status": "paired", "name": name, "port": port,
            "adapter": adapter_name,
            "enabled": entry.get("enabled", True) if isinstance(entry, dict) else True,
            "listen_channel": entry.get("listen_channel", 1) if isinstance(entry, dict) else 1,
        })
    return jsonify({"error": "Pairing failed"}), 500


@bp.route("/api/pairing/remove", methods=["POST"])
def api_remove_device():
    data = request.get_json() or {}
    address = data.get("address", "")
    if not address:
        return jsonify({"error": "No address provided"}), 400

    adapter_name = _resolve_pairing_adapter(data)

    # Disconnect if active
    current_app.device_server.disconnect_device(address)

    # Remove from BlueZ
    current_app.bt_manager.remove_device(address, adapter_name)
    # Remove from config (also releases port)
    current_app.gateway_config.remove_device(address)
    # The removed device may have been the only one using a given channel;
    # close its SPP listener so BlueZ isn't advertising dead services.
    current_app.device_server.refresh_profiles()
    return jsonify({"status": "removed"})


@bp.route("/api/pairing/remove-all", methods=["POST"])
def api_remove_all():
    """Disconnect and unpair all devices."""
    data = request.get_json(silent=True) or {}
    adapter_name = _resolve_pairing_adapter(data)

    # Disconnect all active connections
    current_app.device_server.disconnect_all()

    # Remove all from BlueZ
    devices = current_app.gateway_config.get_devices()
    for address in devices:
        current_app.bt_manager.remove_device(address, adapter_name)

    # Clear config
    current_app.gateway_config.remove_all_devices()
    current_app.device_server.refresh_profiles()
    return jsonify({"status": "all_removed"})


# ── PLC pairing API (PLC adapter, single device only) ───────────────────────

@bp.route("/api/plc/status")
def api_plc_status():
    """Return information about the PLC's paired device (if any)."""
    adapter = current_app.gateway_config.get("plc_adapter", "")
    paired = None
    if adapter:
        paired = current_app.bt_manager.get_single_paired_device(adapter)
    plc_conn = current_app.plc_connection
    effective_channel = 0
    if plc_conn is not None:
        effective_channel = int(getattr(plc_conn, "channel", 0) or 0)
    return jsonify({
        "adapter": adapter,
        "paired": paired,
        "status": plc_conn.status if plc_conn else "disconnected",
        # Configured override (0 = auto-discover).
        "channel": int(current_app.gateway_config.get("plc_channel", 0) or 0),
        # Actual channel currently in use (discovered from the PLC's SDP).
        "effective_channel": effective_channel,
        "com_port": current_app.gateway_config.get("plc_com_port", ""),
        "port": current_app.gateway_config.get("plc_port", 0),
    })


@bp.route("/api/plc/discovery/start", methods=["POST"])
def api_plc_discovery_start():
    adapter = current_app.gateway_config.get("plc_adapter", "")
    if not adapter:
        return jsonify({"error": "No PLC adapter configured"}), 400
    # Only allow one paired PLC
    if current_app.bt_manager.get_single_paired_device(adapter):
        return jsonify({
            "error": "A PLC is already paired on this adapter. "
                     "Unpair it first to scan for a different one."
        }), 400
    current_app.bt_manager.power_adapter(adapter, True)
    current_app.bt_manager.start_discovery(adapter)
    return jsonify({"status": "discovering"})


@bp.route("/api/plc/discovery/stop", methods=["POST"])
def api_plc_discovery_stop():
    adapter = current_app.gateway_config.get("plc_adapter", "")
    if adapter:
        current_app.bt_manager.stop_discovery(adapter)
    return jsonify({"status": "stopped"})


@bp.route("/api/plc/discovered")
def api_plc_discovered():
    adapter = current_app.gateway_config.get("plc_adapter", "")
    if not adapter:
        return jsonify([])
    return jsonify(current_app.bt_manager.list_devices(adapter))


@bp.route("/api/plc/pair", methods=["POST"])
def api_plc_pair():
    """Pair the single PLC on the PLC adapter."""
    data = request.get_json() or {}
    address = (data.get("address") or "").strip()
    if not address:
        return jsonify({"error": "No address provided"}), 400

    adapter = current_app.gateway_config.get("plc_adapter", "")
    if not adapter:
        return jsonify({"error": "No PLC adapter configured"}), 400

    existing = current_app.bt_manager.get_single_paired_device(adapter)
    if existing and existing["address"].upper() != address.upper():
        return jsonify({
            "error": f"A PLC ({existing['address']}) is already paired. "
                     "Unpair it first."
        }), 400

    if not current_app.bt_manager.pair_device(address, adapter):
        return jsonify({"error": "Pairing failed"}), 500

    # Force SPP — kill any audio/HID that BlueZ may have connected
    for uuid in (HID_UUID,
                 "0000110b-0000-1000-8000-00805f9b34fb",
                 "0000110a-0000-1000-8000-00805f9b34fb",
                 "0000111e-0000-1000-8000-00805f9b34fb",
                 "00001108-0000-1000-8000-00805f9b34fb"):
        current_app.bt_manager.disconnect_profile(address, uuid, adapter)
    current_app.bt_manager.connect_profile(address, SPP_UUID, adapter)
    current_app.bt_manager.stop_discovery(adapter)
    # The device we just claimed for the PLC side may have been previously
    # paired on the devices adapter too.  Refresh SPP profile registrations
    # so that device no longer has an active listener.
    try:
        current_app.device_server.refresh_profiles()
    except Exception:
        logger.exception("refresh_profiles after PLC pair failed")
    return jsonify({"status": "paired", "address": address.upper()})


@bp.route("/api/plc/unpair", methods=["POST"])
def api_plc_unpair():
    adapter = current_app.gateway_config.get("plc_adapter", "")
    if not adapter:
        return jsonify({"error": "No PLC adapter configured"}), 400
    paired = current_app.bt_manager.get_single_paired_device(adapter)
    if not paired:
        return jsonify({"status": "no_plc_paired"})
    current_app.bt_manager.remove_device(paired["address"], adapter)
    # If this address is also a paired device, the PLC lockout for that
    # device is now gone — reopen its SPP listener if it's enabled.
    try:
        current_app.device_server.refresh_profiles()
    except Exception:
        logger.exception("refresh_profiles after PLC unpair failed")
    return jsonify({"status": "unpaired", "address": paired["address"]})


# ── Settings API ─────────────────────────────────────────────────────────────

@bp.route("/api/settings", methods=["GET"])
def api_settings_get():
    cfg = current_app.gateway_config.data
    return jsonify({
        "plc_adapter": cfg.get("plc_adapter", ""),
        "device_adapter": cfg.get("device_adapter", ""),
        "plc_channel": int(cfg.get("plc_channel", 0) or 0),
        "plc_com_port": cfg.get("plc_com_port", ""),
        "plc_port": cfg.get("plc_port", 0),
        "plc_reconnect_interval": cfg.get("plc_reconnect_interval", 5),
        "web_port": cfg.get("web_port", 8080),
        "debug_mode": bool(cfg.get("debug_mode", False)),
    })


@bp.route("/api/settings", methods=["POST"])
def api_settings_update():
    data = request.get_json()
    cfg = current_app.gateway_config

    allowed = [
        "plc_adapter", "device_adapter",
        "plc_channel", "plc_com_port", "plc_port", "plc_reconnect_interval",
    ]
    for key in allowed:
        if key in data:
            value = data[key]
            if key in ("plc_channel", "plc_port", "plc_reconnect_interval"):
                try:
                    value = int(value)
                except (TypeError, ValueError):
                    continue
            elif key == "plc_com_port":
                # Accept either "COM6" or "6" — store as a short label.
                value = str(value or "").strip()
            cfg.set(key, value)

    return jsonify({"status": "saved"})


# ── Debug mode API ──────────────────────────────────────────────────────────

@bp.route("/api/debug_mode", methods=["GET"])
def api_debug_mode_get():
    return jsonify({"enabled": bool(current_app.gateway_config.get("debug_mode", False))})


@bp.route("/api/debug_mode", methods=["POST"])
def api_debug_mode_set():
    data = request.get_json() or {}
    enabled = bool(data.get("enabled", False))
    current_app.gateway_config.set("debug_mode", enabled)
    return jsonify({"enabled": enabled})


# ── COM port API ────────────────────────────────────────────────────────────

@bp.route("/api/ports/available")
def api_ports_available():
    address = request.args.get("for", "")
    if address:
        address = address.replace("-", ":").upper()
    ports = current_app.gateway_config.available_ports(
        exclude_address=address or None
    )
    return jsonify({"ports": ports})


@bp.route("/api/devices/<address>/port", methods=["POST"])
def api_set_device_port(address):
    address = address.replace("-", ":").upper()
    data = request.get_json() or {}
    try:
        port = int(data.get("port"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid port"}), 400
    ok = current_app.gateway_config.set_device_port(address, port)
    if not ok:
        return jsonify({"error": "Port unavailable or device unknown"}), 400
    return jsonify({"status": "set", "port": port})


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


# ── Device enable / listen-channel API ──────────────────────────────────────

@bp.route("/api/devices/<address>/enabled", methods=["POST"])
def api_set_device_enabled(address):
    """Toggle whether the gateway accepts SPP connections from this device.

    When enabled=true, the SPP profile for this device's listen channel is
    registered with BlueZ (if not already).  When enabled=false, incoming
    connections from this device are rejected, and the listener for its
    channel is torn down if no other enabled device still uses it.
    """
    address = address.replace("-", ":").upper()
    data = request.get_json() or {}
    enabled = bool(data.get("enabled"))
    if not current_app.gateway_config.set_device_enabled(address, enabled):
        return jsonify({"error": "Device not found"}), 404
    # If we just disabled an active connection, drop it now.
    if not enabled:
        current_app.device_server.disconnect_device(address)
    current_app.device_server.refresh_profiles()
    return jsonify({"status": "ok", "enabled": enabled})


@bp.route("/api/devices/<address>/listen-channel", methods=["POST"])
def api_set_device_listen_channel(address):
    """Set the RFCOMM channel this device's SPP listener uses (1-30).

    The scanner / remote Pi firmware is normally configured to hit a
    specific channel (the Windows "COM port" of the remote side), so each
    device may want its own listen channel on the gateway.  Channel
    uniqueness is enforced — two enabled devices cannot share a channel.
    """
    address = address.replace("-", ":").upper()
    data = request.get_json() or {}
    try:
        channel = int(data.get("channel"))
    except (TypeError, ValueError):
        return jsonify({"error": "Invalid channel"}), 400
    if not current_app.gateway_config.set_device_listen_channel(address, channel):
        return jsonify({
            "error": ("Channel must be an integer between 1 and 30 and must "
                      "not already be assigned to another enabled device.")
        }), 400
    current_app.device_server.refresh_profiles()
    return jsonify({"status": "ok", "listen_channel": channel})


# ── HID → SPP handover API ──────────────────────────────────────────────────

@bp.route("/api/devices/<address>/handover/start", methods=["POST"])
def api_handover_start(address):
    """Hold the scanner connected in HID mode while the SPP listener is
    armed, so the user can scan the vendor 'switch to SPP' barcode on
    a live ACL link and have the mode change land on us reliably.
    """
    address = address.replace("-", ":").upper()
    if not current_app.device_server.start_handover(address):
        return jsonify({"error": "Device is not paired on this adapter"}), 400
    return jsonify({"status": "handover_started", "address": address})


@bp.route("/api/devices/<address>/handover/stop", methods=["POST"])
def api_handover_stop(address):
    address = address.replace("-", ":").upper()
    current_app.device_server.stop_handover(address, reason="user.cancel")
    return jsonify({"status": "handover_stopped", "address": address})


@bp.route("/api/handover/active")
def api_handover_active():
    return jsonify({
        "addresses": current_app.device_server.active_handovers,
    })


# ── Connection log API ──────────────────────────────────────────────────────

@bp.route("/api/connection-log")
def api_connection_log():
    """Return the recent connection-log entries as JSON.

    Query params:
        address — filter to a single BT MAC (case-insensitive)
        limit   — cap on number of entries returned (most recent)
    """
    clog = getattr(current_app, "conn_log", None)
    if clog is None:
        return jsonify({"entries": []})
    address = request.args.get("address", "").strip()
    limit_raw = request.args.get("limit", "").strip()
    limit = None
    if limit_raw:
        try:
            limit = max(1, min(10000, int(limit_raw)))
        except ValueError:
            limit = None
    return jsonify({"entries": clog.entries(address=address or None,
                                            limit=limit)})


@bp.route("/api/connection-log/download")
def api_connection_log_download():
    """Download the connection log as a plain-text attachment."""
    clog = getattr(current_app, "conn_log", None)
    body = clog.to_text() if clog is not None else "(no connection log)\n"
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y%m%dT%H%M%SZ"
    )
    filename = f"bt-gateway-connection-log-{stamp}.txt"
    return Response(
        body,
        mimetype="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


@bp.route("/api/connection-log/clear", methods=["POST"])
def api_connection_log_clear():
    clog = getattr(current_app, "conn_log", None)
    cleared = clog.clear() if clog is not None else 0
    return jsonify({"status": "ok", "cleared": cleared})


# ── Restart (for wizard adapter re-selection) ───────────────────────────────

def _restart_process():
    """Re-exec the current Python process.

    Invoked from a short-lived background thread so the HTTP response has
    time to reach the client before the process image is replaced.
    """
    logger.info("Restarting process via os.execv")
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        logger.error("os.execv failed, exiting so supervisor can restart: %s", e)
        os._exit(0)


@bp.route("/api/restart", methods=["POST"])
def api_restart():
    """Relaunch the gateway process.

    Used by the setup wizard after changing adapters or other settings
    that need a full restart to take effect.  The response is sent first;
    the restart happens 0.5s later on a background thread.
    """
    def _go():
        import time as _t
        _t.sleep(0.5)
        _restart_process()

    threading.Thread(target=_go, daemon=True, name="restart").start()
    return jsonify({"status": "restarting"})
