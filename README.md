# BT Gateway

Bluetooth SPP gateway for Raspberry Pi 5. Bridges multiple Bluetooth devices
to a PLC over Serial Port Profile (SPP), with a web-based management interface.

## Architecture

```
 Device 1 ──SPP──┐                          ┌──SPP──> PLC
 Device 2 ──SPP──┤  [Device Adapter]  [PLC Adapter]   │
 Device N ──SPP──┘       │  Pi 5  │          └─────────┘
                     Web UI :8080
```

- **Two BT adapters** — one for devices, one for PLC
- **Device → PLC**: raw data is wrapped in JSON `{"device_id":"connectionN","message":"..."}`
- **PLC → Device**: JSON is parsed and routed to the named device
- **PLC connection**: Pi initiates, auto-reconnects forever
- **Device connections**: devices initiate connections to the Pi
- **Web UI**: real-time dashboard, pairing page, adapter/PLC settings

## Quick Start (Docker)

```bash
# One-time host setup (disables BlueZ HID plugin, enables docker at
# boot, adds a desktop autostart entry that opens the web UI).
sudo ./scripts/install-host.sh

# Build and run the gateway
docker compose up -d --build

# View logs
docker compose logs -f bt-gateway
```

The web interface is available at `http://<pi-ip>:8080` and will also
pop open automatically when you log into the Pi's desktop. The
`restart: unless-stopped` policy in `docker-compose.yml` keeps the
container running across reboots and crashes.

### First-time Setup

1. Open the web UI and go to **Settings** (or the **Setup Wizard**)
2. Select the **PLC Adapter** and **Device Adapter** from detected hardware
3. Enter the PLC's Windows COM port label (e.g. `COM6`). The RFCOMM channel
   is auto-discovered from the PLC's SDP — no need to set it manually
4. Save and restart the container: `docker compose restart`
5. Go to **Pairing**, enable pairing mode, and pair your devices

### Honeywell 8675i (and other HID-capable scanners)

Barcode scanners typically ship in **Bluetooth HID Keyboard** mode by
default, which means scanned data is delivered to the host as
keystrokes, not as serial data — you'll see URL barcodes opening a
browser rather than showing up in the Message Log.

To use the scanner with this gateway you must switch it to **Bluetooth
SPP mode** by scanning the "Serial Port Profile" programming barcode
from the scanner's User's Guide. After that:

- The scanner drops its HID profile and only advertises SPP
- The gateway's auto-connect loop will bring up SPP every few seconds
  without any desktop-side "Connect" click
- Scanned data shows up in the Message Log (and is forwarded to the PLC)

`scripts/install-host.sh` also disables BlueZ's HID input plugin as a
belt-and-suspenders measure, so even if the scanner is still in HID
mode it can't generate keystrokes on the Pi.

## Running Without Docker

```bash
# Install system dependencies (Raspberry Pi OS)
sudo apt-get install -y bluez python3-dbus python3-gi python3-dev \
    libdbus-1-dev libglib2.0-dev libgirepository1.0-dev

# Install Python packages
pip install -r requirements.txt

# Run (needs root for Bluetooth access)
sudo CONFIG_PATH=./config.json python run.py
```

## Configuration

The config file (`/data/config.json` in Docker, or `CONFIG_PATH` env var) stores:

| Key | Description |
|-----|-------------|
| `plc_adapter` | BlueZ adapter name for PLC (e.g. `hci0`) |
| `device_adapter` | BlueZ adapter name for devices (e.g. `hci1`) |
| `plc_address` | PLC Bluetooth MAC address |
| `plc_channel` | RFCOMM channel for PLC SPP connection |
| `plc_reconnect_interval` | Seconds between PLC reconnect attempts |
| `web_host` | Web server bind address |
| `web_port` | Web server port |
| `devices` | Map of paired device addresses to names |

## Message Protocol

Messages between the gateway and PLC are **newline-delimited JSON** over SPP:

```json
{"device_id": "connection1", "message": "raw payload from/to device"}
```

- `device_id`: the connection name assigned to the device (configurable in the UI)
- `message`: the raw data string

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CONFIG_PATH` | `/data/config.json` | Path to configuration file |
| `LOG_LEVEL` | `INFO` | Logging level (DEBUG, INFO, WARNING, ERROR) |

## Project Structure

```
bt_gateway/
  config.py          — Configuration management
  bt_manager.py      — BlueZ D-Bus adapter control
  plc_connection.py  — PLC SPP client (auto-reconnect)
  device_server.py   — Per-scanner RFCOMM-over-TTY client managers
  rfcomm_tty.py      — /dev/rfcomm<N> bind/release helpers (rfcomm(1))
  message_router.py  — Routes messages between PLC and devices
  web/
    app.py           — Flask application factory
    routes.py        — HTTP + API endpoints
    templates/       — Jinja2 HTML templates
    static/          — CSS and JavaScript
Dockerfile
docker-compose.yml
scripts/
  install-host.sh    — One-time host setup (BlueZ HID disable,
                       docker-at-boot, desktop autostart for web UI)
run.py               — Entry point
```