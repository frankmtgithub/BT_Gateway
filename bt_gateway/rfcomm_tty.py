"""Helpers for Linux RFCOMM TTY management (``/dev/rfcomm<N>``).

We use the kernel's RFCOMM TTY subsystem — the same path Windows uses for
"virtual COM ports" — so each paired scanner is exposed as a real
character device.  The gateway's per-device manager then opens that TTY,
which is what actually drives the RFCOMM dial on Linux (equivalent to
Hercules opening COM6 on Windows: the act of opening the port is what
makes the OS initiate the outbound link).

All binding operations are done through the ``rfcomm(1)`` utility shipped
with BlueZ — the same way ``sdp_find_spp_channel`` shells out to
``sdptool``.  This keeps the Python side free of fragile ctypes struct
layouts for ``rfcomm_dev_req``.
"""

import logging
import os
import re
import shutil
import subprocess
import time

logger = logging.getLogger(__name__)


def have_rfcomm():
    return bool(shutil.which("rfcomm"))


def device_path(port):
    return f"/dev/rfcomm{int(port)}"


def bind(port, address, channel, adapter=None):
    """Bind ``/dev/rfcomm<port>`` to ``address`` RFCOMM ``channel``.

    If ``adapter`` is given (e.g. ``"hci1"``) the binding uses that
    adapter as the source — equivalent to ``rfcomm -i hci1 bind …`` on
    the CLI.  Without it the kernel picks an adapter by BlueZ's default
    policy, which on a multi-adapter Pi may not be the one the scanner
    was paired on.

    Idempotent — if the binding already matches we return True without
    touching anything; if it exists with a different target we release it
    first so the new one can take effect.
    """
    address = address.upper()
    channel = int(channel)

    current = get_binding(port)
    if current == (address, channel):
        return True
    if current is not None:
        release(port)

    cmd = ["rfcomm"]
    if adapter:
        cmd += ["-i", str(adapter)]
    cmd += ["bind", str(int(port)), address, str(channel)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logger.warning(
            "rfcomm bind %d %s %d (adapter=%s) failed: %s",
            port, address, channel, adapter or "default",
            result.stderr.strip(),
        )
        return False

    # The ``rfcomm bind`` call returns the moment the kernel has accepted
    # the binding, but the ``/dev/rfcomm<N>`` node is created a tick
    # later by devtmpfs / udev.  In Docker with ``/dev`` bind-mounted
    # from the host that propagation can take noticeably longer than
    # on bare metal, and if we just released the same port a moment
    # ago it's even slower.  Poll briefly so callers can rely on the
    # TTY actually being usable when we return True.
    deadline = time.monotonic() + 2.0
    while not tty_exists(port):
        if time.monotonic() >= deadline:
            logger.warning(
                "rfcomm bind %d %s %d (adapter=%s): TTY node "
                "/dev/rfcomm%d did not appear within 2s",
                port, address, channel, adapter or "default", port,
            )
            return False
        time.sleep(0.05)

    logger.info("Bound /dev/rfcomm%d → %s channel %d via %s",
                port, address, channel, adapter or "default adapter")
    return True


def release(port):
    """Release the binding on ``/dev/rfcomm<port>``."""
    result = subprocess.run(
        ["rfcomm", "release", str(int(port))],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.debug(
            "rfcomm release %d failed: %s",
            port, result.stderr.strip(),
        )
        return False
    return True


def release_all():
    """Clear every current binding.  Used at startup so we start from a
    clean slate regardless of what a previous run (or the system) left
    lying around."""
    subprocess.run(
        ["rfcomm", "release", "all"],
        capture_output=True,
        text=True,
    )


def get_binding(port):
    """Return ``(address, channel)`` for ``/dev/rfcomm<port>``, or ``None``
    if the port is not bound."""
    port = int(port)
    for entry in list_bindings():
        if entry["port"] == port:
            return (entry["address"], entry["channel"])
    return None


_RFCOMM_LINE = re.compile(
    r"rfcomm(\d+):\s+([0-9A-Fa-f:]{17})\s+channel\s+(\d+)\s+(\S+)"
)


def list_bindings():
    """Parse ``rfcomm -a`` output into a list of binding dicts."""
    result = subprocess.run(
        ["rfcomm", "-a"],
        capture_output=True,
        text=True,
    )
    entries = []
    if result.returncode != 0:
        return entries
    for line in result.stdout.splitlines():
        m = _RFCOMM_LINE.match(line.strip())
        if m:
            entries.append({
                "port": int(m.group(1)),
                "address": m.group(2).upper(),
                "channel": int(m.group(3)),
                "state": m.group(4),
            })
    return entries


def tty_exists(port):
    return os.path.exists(device_path(port))
