#!/usr/bin/env bash
# install-host.sh — one-shot Raspberry Pi host setup for BT Gateway.
#
# What this does on the host (NOT inside the container):
#
#   1. Disables BlueZ's HID input plugin so scanners that are still in
#      Bluetooth-keyboard mode can't deliver keystrokes to the desktop
#      (which is why scanning a URL barcode opens a browser).  The
#      scanner itself still has to be switched to SPP mode via its
#      vendor programming barcode — see the README — but this prevents
#      the desktop-keyboard side-effect in the meantime.
#
#   2. Ensures Docker is enabled at boot and the bt-gateway container
#      comes up automatically (the compose file already has
#      `restart: unless-stopped`, we just make sure dockerd is enabled).
#
#   3. Installs a desktop autostart entry that opens the web UI in the
#      default browser when the desktop session starts — only if the
#      UI isn't already open on port 8080.
#
# Run from the repo root:
#
#     sudo ./scripts/install-host.sh
#
# Re-runnable; it's idempotent.

set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "This script must be run with sudo (needs to edit /etc/systemd)." >&2
    exit 1
fi

# Resolve the target user for the desktop autostart piece.  If invoked
# with sudo, $SUDO_USER is the real user; otherwise try the owner of
# /home/pi or fall back to the invoking user.
TARGET_USER="${SUDO_USER:-}"
if [[ -z "${TARGET_USER}" ]] && [[ -d /home/pi ]]; then
    TARGET_USER="pi"
fi
if [[ -z "${TARGET_USER}" ]]; then
    TARGET_USER="$(logname 2>/dev/null || whoami)"
fi
TARGET_HOME="$(getent passwd "${TARGET_USER}" | cut -d: -f6)"

echo "== BT Gateway host installer"
echo "   target desktop user: ${TARGET_USER} (${TARGET_HOME})"
echo

# ── 1. bluetoothd --noplugin=input ───────────────────────────────────
echo "[1/3] Disabling BlueZ HID (input) plugin so scanners can't"
echo "      deliver barcodes as keystrokes to the desktop."

BLUEZ_DROPIN_DIR="/etc/systemd/system/bluetooth.service.d"
BLUEZ_DROPIN="${BLUEZ_DROPIN_DIR}/10-bt-gateway-nohid.conf"

mkdir -p "${BLUEZ_DROPIN_DIR}"

# Find the real bluetoothd binary (the path varies: /usr/lib/bluetooth/
# on Debian/Raspbian, /usr/libexec/bluetooth/ on some others).
BLUETOOTHD=""
for candidate in /usr/libexec/bluetooth/bluetoothd \
                 /usr/lib/bluetooth/bluetoothd \
                 /usr/sbin/bluetoothd; do
    if [[ -x "${candidate}" ]]; then
        BLUETOOTHD="${candidate}"
        break
    fi
done
if [[ -z "${BLUETOOTHD}" ]]; then
    echo "   !! Could not find bluetoothd binary — skipping HID disable." >&2
else
    cat > "${BLUEZ_DROPIN}" <<EOF
# Installed by BT Gateway (scripts/install-host.sh).
# Drops the HID input plugin so scanners in HID mode can't generate
# OS keystrokes on the Pi.  Remove this file and run
#   systemctl daemon-reload && systemctl restart bluetooth
# to revert.
[Service]
ExecStart=
ExecStart=${BLUETOOTHD} --noplugin=input
EOF
    systemctl daemon-reload
    systemctl restart bluetooth
    echo "      wrote ${BLUEZ_DROPIN}"
    echo "      restarted bluetooth.service"
fi
echo

# ── 2. Docker at boot ────────────────────────────────────────────────
echo "[2/3] Enabling Docker at boot so the container auto-starts."
if systemctl list-unit-files | grep -q '^docker\.service'; then
    systemctl enable --now docker >/dev/null
    echo "      docker.service enabled"
else
    echo "      docker.service not found — install Docker first." >&2
fi
echo

# ── 3. Desktop autostart for the web UI ─────────────────────────────
echo "[3/3] Installing desktop autostart entry for the web UI."

AUTOSTART_DIR="${TARGET_HOME}/.config/autostart"
AUTOSTART_FILE="${AUTOSTART_DIR}/bt-gateway-ui.desktop"
LAUNCHER_DIR="${TARGET_HOME}/.local/bin"
LAUNCHER="${LAUNCHER_DIR}/bt-gateway-open-ui.sh"

mkdir -p "${AUTOSTART_DIR}" "${LAUNCHER_DIR}"

# Launcher: wait until the Flask port is accepting connections, then
# open the default browser unless a window is already on that URL.
cat > "${LAUNCHER}" <<'EOF'
#!/usr/bin/env bash
# Opens the BT Gateway web UI once the container is accepting traffic.
URL="http://localhost:8080/"

# Wait up to 60s for the port to come up.
for _ in $(seq 1 60); do
    if (exec 3<>/dev/tcp/127.0.0.1/8080) 2>/dev/null; then
        exec 3<&- 3>&-
        break
    fi
    sleep 1
done

# Avoid opening a second tab if the user already has the UI up.
if command -v xdotool >/dev/null 2>&1; then
    if xdotool search --name "BT Gateway" >/dev/null 2>&1; then
        exit 0
    fi
fi

xdg-open "${URL}" >/dev/null 2>&1 || true
EOF
chmod +x "${LAUNCHER}"
chown "${TARGET_USER}:${TARGET_USER}" "${LAUNCHER}"

cat > "${AUTOSTART_FILE}" <<EOF
[Desktop Entry]
Type=Application
Name=BT Gateway Web UI
Comment=Open the BT Gateway web interface at http://localhost:8080
Exec=${LAUNCHER}
Terminal=false
X-GNOME-Autostart-enabled=true
# Small delay so the desktop has settled before we open a browser.
X-GNOME-Autostart-Delay=5
EOF
chown -R "${TARGET_USER}:${TARGET_USER}" \
    "${TARGET_HOME}/.config/autostart" "${TARGET_HOME}/.local/bin"
echo "      wrote ${AUTOSTART_FILE}"
echo "      wrote ${LAUNCHER}"
echo

echo "Done."
echo
echo "Next steps:"
echo "  * From the repo root:   docker compose up -d --build"
echo "  * Reboot once so the BlueZ change and autostart entry take effect."
echo "  * Put your Honeywell 8675i scanner into Bluetooth SPP mode by"
echo "    scanning the \"Serial Port Profile\" programming barcode from"
echo "    the scanner's User's Guide. Until then the scanner stays in"
echo "    keyboard mode and will not send data over SPP."
