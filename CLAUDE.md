# BT Gateway — Claude working notes

This file is maintained by Claude to preserve session-to-session context
about the scanner-connection investigation.  Summarise decisions here so
we don't re-derive them every session.

## High-level goal

Raspberry Pi 5 hosts a BlueZ-based SPP gateway (`bt_gateway/`).  Two
sides:

* **PLC side** — one adapter, one paired device (a Windows host running
  a virtual COM port).  Gateway acts as RFCOMM client; SDP-discovers the
  channel and opens a socket.  Already working.
* **Devices side** — one adapter, N paired scanners / remote Pis.
  Gateway acts as RFCOMM server (Profile1 on BlueZ), scanners initiate
  the SPP connection to a per-device listen channel.  This is the flaky
  path the user is debugging.

## What's in place

* `device_server.DeviceServer` registers one `SPPProfile` per in-use
  RFCOMM channel and keeps a per-address `DeviceConnection`.
* Each paired device has a `listen_channel` (1–30) and an `enabled`
  flag (`bt_gateway/config.py`).
* Auto-connect loop in `DeviceServer` tries `ConnectProfile(SPP)` +
  `DisconnectProfile(HID)` on every enabled paired device every 10 s.
* `check_connection_allowed()` gates the `NewConnection` callback on
  pairing status, enabled flag, and channel match.
* `pairing_agent.PairingAgent` auto-approves every pairing request
  (Just Works).

## Known-working flow for the user

Scanners arrive from the factory in HID mode.  The user pairs them in
HID mode (keyboard), then scans a vendor "switch to SPP" barcode.  The
scanner reboots its BT stack in SPP mode and tries to open RFCOMM to
the last-paired host on some channel.  Intermittently, the scanner
can't find a listening SPP endpoint and gives up.

## Session 2026-04-16 — HID→SPP handover + connection log

User's ask:
1. Let the app explicitly hold the HID connection **active** while
   the user scans the mode-change barcode.  That keeps the ACL link
   up so the scanner's transition from HID to SPP happens on an
   already-awake link.
2. Guarantee the SPP listener for **exactly that device on exactly
   that channel** is registered before, during, and after the
   handover — nothing else should be able to claim the channel.
3. Add a live connection-log window that traces every step of the
   pairing / handover / reconnection path so the user can hand the
   log back to us when a scanner refuses to come up.
4. Add a "download log" button to export the buffer as a text file.

### Changes made

* New `bt_gateway/connection_log.py` — thread-safe ring buffer
  (default 2 000 entries) with Socket.IO emission.  `log(level, step,
  address, detail, **extras)` is the single entry point.  Used
  from `device_server`, `bt_manager`, `pairing_agent`, `routes`.
* `device_server.py` annotated heavily: every profile
  registration, every `NewConnection` / `Release`, every
  auto-connect tick, every rejection in `check_connection_allowed`
  now writes a `ConnectionLog` entry.
* New `DeviceServer.start_handover(address)` /
  `stop_handover(address)` — brings the device up via BlueZ
  `Device1.Connect()` (which tries every known profile including
  HID), keeps polling to make sure HID stays up, and refreshes the
  SPP profile for the device's listen channel so the scanner lands
  on us when it flips to SPP.
* `bt_manager.connect_device()` — thin wrapper around `Device1.Connect`
  used by the handover flow.
* `config.set_device_listen_channel()` now rejects channels already
  claimed by another enabled paired device.  Each device owns its
  channel exclusively.
* `check_connection_allowed()` also rejects connections whose
  channel is already actively in use by a different address — the
  per-channel lock is enforced at runtime too, not just at config
  time.
* `routes.py` exposes:
  - `POST /api/devices/<addr>/handover/start`
  - `POST /api/devices/<addr>/handover/stop`
  - `GET  /api/connection-log` (JSON list)
  - `GET  /api/connection-log/download` (text/plain attachment)
  - `POST /api/connection-log/clear`
* `templates/pairing.html` gained a "Connection Log" card with live
  Socket.IO stream, filter-by-address, clear and download buttons,
  plus a "Prepare for SPP mode" action on every paired device row.
* `web/static/js/app.js` subscribes to the `connection_log` event
  globally so the log keeps streaming even when the user is on the
  Dashboard.

### Conventions

* All ConnectionLog entries carry `{ts, level, step, address,
  detail, channel?}`.  `level` is `info | warn | error | debug`;
  `step` is a short code like `profile.register`, `hid.connect`,
  `spp.newconnection`, `spp.rejected`, `auto.tick`, `handover.start`.
* When adding a new path, prefer a new `step` over stuffing data
  into `detail`.  `detail` is the human-readable sentence; `step`
  is what the UI filters and groups by.

## Repo pointers

* Entry point: `run.py`
* BT adapter layer: `bt_gateway/bt_manager.py`
* SPP server + handover: `bt_gateway/device_server.py`
* Connection log: `bt_gateway/connection_log.py`
* PLC RFCOMM client: `bt_gateway/plc_connection.py`
* Web UI: `bt_gateway/web/templates/pairing.html` (connection log
  lives there), `bt_gateway/web/static/js/app.js`
* Config store: `bt_gateway/config.py` (JSON at
  `/data/config.json`)
