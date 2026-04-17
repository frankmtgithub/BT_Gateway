# BT Gateway — Claude working notes

This file is maintained by Claude to preserve session-to-session context
about the scanner-connection investigation.  Summarise decisions here so
we don't re-derive them every session.

## High-level goal

Raspberry Pi 5 hosts a BlueZ-based SPP gateway (`bt_gateway/`).  Two
sides, both run the gateway as the RFCOMM **client**:

* **PLC side** — one adapter, one paired device (a Windows host running
  a virtual COM port).  Gateway dials into it via an `AF_BLUETOOTH`
  socket on the SDP-discovered SPP channel.
* **Devices side** — one adapter, N paired scanners.  For each enabled
  paired scanner the gateway binds `/dev/rfcomm<N>` to the scanner's
  address + channel and opens the TTY — opening the TTY is what makes
  the kernel dial out over RFCOMM, mirroring Windows's "opening COM6
  triggers the SPP connect".  One `_DeviceLink` manager thread per
  scanner keeps the TTY open, redials on drop, and pushes data into the
  router.

## What's in place

* `device_server._DeviceLink` — one per enabled paired scanner.  Binds
  `/dev/rfcomm<port>` via `bt_gateway.rfcomm_tty.bind`, opens the TTY in
  raw mode, read-loops it, and registers itself with the router so
  `route_from_device` can push scans to the PLC.
* `device_server.DeviceServer` — tracks the set of `_DeviceLink`
  managers; `refresh_managers()` aligns them with
  `config.get_enabled_devices()` whenever the config changes.
* Each paired device has a `port` (`/dev/rfcomm<N>` assignment) and an
  optional `listen_channel` override used only when SDP discovery fails.
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

## Session 2026-04-16 (late) — keep HID alive until barcode-switch

The "force SPP, kill HID" logic on the devices side was too eager:
it tore HID down right after `Pair()` and again on every 10 s
auto-connect tick, which meant a scanner that had just been paired
in keyboard mode got kicked off the link before the operator could
scan the vendor "switch to SPP" barcode.

New contract — mirrors the Windows flow the user is used to:

* `Pair()` succeeds → scanner stays connected as HID.  No
  `DisconnectProfile(HID)`, no `ConnectProfile(SPP)` at pair time.
* Operator scans the switch-to-SPP barcode → scanner reboots and
  initiates RFCOMM into our listener on its **configured**
  `listen_channel` (per-device, deterministic — that's the
  "same /dev/rfcomm<N> every time" invariant).
* `_on_new_connection` (SPP accepted) → that's when we drop HID on
  the scanner so barcode reads don't leak to the Pi desktop as
  keystrokes.
* Auto-connect loop: nudges `ConnectProfile(SPP)` on disconnected
  paired devices but **no longer** calls `DisconnectProfile(HID)`.
  For devices already on SPP we keep the periodic HID-off call as
  a safety net in case BlueZ opportunistically re-raised HID.

Call sites touched: `routes.api_pair_device`,
`device_server._auto_connect_tick` (disconnected branch).  Handover
flow (`start_handover` / "Prepare for SPP mode" button) is
unchanged — still available for troubleshooting scanners that
won't come up on their own.

## Session 2026-04-16 (later) — the HID keepalive can't work on this Pi

User reported scanners still dropping the HID link right after
`Pair()`.  The log shows the scanner's UUID list is populated but
`Device1.Connect` fails every time with
`org.bluez.Error.NotAvailable: br-connection-profile-unavailable`,
which is BlueZ's way of saying "I opened the ACL but none of my
loaded profile plugins match any UUID on the remote, so I have
nowhere to put this connection".

Root cause: on this Raspberry Pi, BlueZ is running without the
**input** (HID) plugin loaded.  Without that plugin, BlueZ has no
HID host and cannot keep a scanner that's paired in keyboard mode
awake — the scanner's BT radio sleeps within seconds of `Pair()`
completing because nothing on the Pi is subscribing to its HID
reports.  `start_handover` (calling `Device1.Connect()` on a loop)
can't fix that: every call fails with the same error.

Implications for the app:

* **Don't call `start_handover` automatically after pair.**  It
  just loudly fails for 90 s and gives the operator misleading
  "keepalive tick N: ACL down" entries.  Call it manually from
  the Pairing page only when the user really wants to try.
* **Expose the scanner's UUIDs in the connection log after pair.**
  `pair.uuids` step prints the full list so the user can see
  whether SPP is already advertised.  If SPP is there we kick
  `ConnectProfile(SPP)` immediately (the scanner is briefly awake
  right after `Pair()`).  If only HID is there, we warn the user
  that on this Pi the scanner needs to be switched to SPP mode
  first (scan the vendor setup barcode before re-pairing).
* **Operator-facing flow that actually works on this Pi**: power
  on the scanner, scan the vendor "switch to SPP" setup barcode
  while unpaired (no host needed — it's a firmware command),
  power-cycle the scanner, then pair.  The scanner will now
  advertise SPP directly, `Device1.Connect` will bring it up via
  our SPP profile, and the auto-connect loop will keep it alive.
* **Alternative if the user really wants HID-first onboarding**:
  enable BlueZ's `input` plugin in `/etc/bluetooth/main.conf`
  (out of scope for this app — system-level change).

## Session 2026-04-16 (cleanup) — drop the HID→SPP handover code

The `start_handover` / `stop_handover` flow and its UI entry
(`Prepare for SPP mode` button, handover modal) were removed
entirely.  They can't work on this Pi — `Device1.Connect` fails
with `br-connection-profile-unavailable` for HID-only scanners
because BlueZ has no `input` plugin loaded — and keeping them
around just confused operators.

Also silenced the PLC reconnect loop in the **connection log**
UI: `bt_manager.connect_profile` and `disconnect_profile` now
accept a `silent=False` kwarg, and `plc_connection._prepare_plc_link`
/ `_close_socket` pass `silent=True`.  The stdlib Python logger
still records everything; the user-facing connection-log panel is
back to being about scanner events only.

Removed:

* `DeviceServer.start_handover` / `stop_handover` /
  `_stop_handover_unlocked` / `_handover_loop` /
  `active_handovers` property.
* `BtManager.connect_device` (only caller was the handover flow).
* `/api/devices/<addr>/handover/start|stop` and
  `/api/handover/active` routes.
* Handover modal, `startHandover()` JS, and the "Prepare for SPP
  mode" button in `pairing.html`.
* Handover-related bookkeeping in `_auto_connect_tick` and
  `accept_connection`.

## Session 2026-04-17 — flip the devices side to RFCOMM-client-over-TTY

Instead of the Pi listening for incoming SPP (Profile1 server) from the
scanners, the gateway now dials **out** to each scanner, exactly the
way Windows's virtual COM ports do — and exactly the way the PLC side
already works.  The user's mental model: *"On Windows I open COM6 in
Hercules and it dials out to the device.  We need the same for
scanners."*

Design:

* `bt_gateway/rfcomm_tty.py` — thin wrapper around the `rfcomm(1)` CLI
  (`bind`, `release`, `release all`, parse `rfcomm -a`).  Gives us real
  `/dev/rfcomm<N>` character devices bound to each scanner.
* `device_server._DeviceLink` — per-scanner manager thread.  Binds
  `/dev/rfcomm<port>`, `os.open`s it (that's what triggers the RFCOMM
  dial; the TTY is in `tty.setraw` mode for 8-bit clean, no echo),
  read-loops bytes, splits on `\n`, pushes to `router.route_from_device`.
  Backoff ladder (5/10/20/40/60 s) on failed dials so an off-range
  scanner doesn't thrash.
* `device_server.DeviceServer.refresh_managers()` — replaces the old
  `refresh_profiles()`.  Spins up / stops / restarts `_DeviceLink`s to
  match `config.get_enabled_devices()`.  Kept as `refresh_profiles()`
  alias for route compatibility.
* `DeviceServer.start()` calls `rfcomm_tty.release_all()` at boot so
  stale bindings from a previous run don't pin scanners to wrong
  channels.
* Per-device channel is discovered via
  `bt_manager.sdp_find_spp_channel`; the saved `listen_channel` in
  config is only the fallback when SDP fails.

Removed:

* `SPPProfile` class, `NewConnection` / `RequestDisconnection` /
  `Release` callbacks, `check_connection_allowed` gate, per-channel
  profile (re)registration, the whole auto-connect loop, and the old
  `DeviceConnection` socket wrapper.
* Manual `ConnectProfile(SPP)` nudge in `routes.api_pair_device` —
  the manager now handles dialing on its own the moment
  `refresh_managers()` sees the new paired device.

## Repo pointers

* Entry point: `run.py`
* BT adapter layer: `bt_gateway/bt_manager.py`
* Scanner RFCOMM-client managers + `/dev/rfcomm<N>` TTYs:
  `bt_gateway/device_server.py` + `bt_gateway/rfcomm_tty.py`
* Connection log: `bt_gateway/connection_log.py`
* PLC RFCOMM client: `bt_gateway/plc_connection.py`
* Web UI: `bt_gateway/web/templates/pairing.html` (connection log
  lives there), `bt_gateway/web/static/js/app.js`
* Config store: `bt_gateway/config.py` (JSON at
  `/data/config.json`)
