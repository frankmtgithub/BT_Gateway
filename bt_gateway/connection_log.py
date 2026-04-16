"""Structured connection log for BT Gateway.

A thread-safe ring buffer of connection-related events with live
Socket.IO emission.  Used by the scanner/SPP and pairing code paths to
trace every step of a connection attempt so the user can correlate
scanner behaviour with gateway decisions.

Entries are dicts with a stable shape so the UI can group / filter
on them:

    {
        "ts": "2026-04-16T18:32:01.123456+00:00",
        "level": "info" | "warn" | "error" | "debug",
        "step":  "handover.start",
        "address": "AA:BB:CC:DD:EE:FF" or "",
        "channel": 1 or None,
        "detail": "human-readable sentence",
        "extras": {...}   # optional, arbitrary key/value pairs
    }

``step`` is a dotted short code so the UI can colour-code groups
(``profile.*``, ``hid.*``, ``spp.*``, ``auto.*``, ``handover.*``).
"""

from __future__ import annotations

import collections
import datetime
import io
import logging
import threading
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

DEFAULT_CAPACITY = 2000


class ConnectionLog:
    """Ring-buffered connection event log with real-time Socket.IO fanout."""

    def __init__(self, capacity: int = DEFAULT_CAPACITY, socketio=None):
        self._entries: collections.deque[Dict[str, Any]] = collections.deque(
            maxlen=capacity
        )
        self._lock = threading.Lock()
        self._socketio = socketio

    # ── Recording ─────────────────────────────────────────────────────

    def log(
        self,
        step: str,
        detail: str,
        *,
        level: str = "info",
        address: str = "",
        channel: Optional[int] = None,
        **extras: Any,
    ) -> Dict[str, Any]:
        """Record an event and emit it to subscribers.

        Returns the entry dict for callers that want to inspect what was
        emitted.  All keyword arguments beyond the named ones are folded
        into ``extras`` so UI surfaces can render them alongside the
        detail string.
        """
        entry = {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "level": level,
            "step": step,
            "address": (address or "").upper() if address else "",
            "channel": int(channel) if channel is not None else None,
            "detail": detail,
            "extras": extras or {},
        }
        with self._lock:
            self._entries.append(entry)

        # Also send into the Python logger so it shows up in the systemd
        # journal / docker logs for post-mortem.
        py_msg = f"[{step}] {detail}"
        if entry["address"]:
            py_msg = f"{entry['address']} {py_msg}"
        if channel is not None:
            py_msg = f"{py_msg} (ch {channel})"
        _log_fn = getattr(logger, level if level in ("info", "warning",
                                                    "error", "debug") else "info")
        if level == "warn":
            _log_fn = logger.warning
        _log_fn("%s", py_msg)

        if self._socketio is not None:
            try:
                self._socketio.emit("connection_log", entry, namespace="/")
            except Exception:
                # Never let a broken Socket.IO client kill the caller.
                logger.exception("connection_log emit failed")
        return entry

    def info(self, step, detail, **kw):
        return self.log(step, detail, level="info", **kw)

    def warn(self, step, detail, **kw):
        return self.log(step, detail, level="warn", **kw)

    def error(self, step, detail, **kw):
        return self.log(step, detail, level="error", **kw)

    def debug(self, step, detail, **kw):
        return self.log(step, detail, level="debug", **kw)

    # ── Query ─────────────────────────────────────────────────────────

    def entries(self, address: Optional[str] = None,
                limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Return a snapshot of the buffer, optionally filtered by address."""
        addr = (address or "").upper()
        with self._lock:
            items: Iterable[Dict[str, Any]] = list(self._entries)
        if addr:
            items = [e for e in items if e.get("address", "") == addr]
        if limit is not None:
            items = list(items)[-int(limit):]
        return list(items)

    def clear(self) -> int:
        """Empty the buffer and return how many entries were discarded."""
        with self._lock:
            n = len(self._entries)
            self._entries.clear()
        self.log("log.cleared", f"Connection log cleared ({n} entries)",
                 level="info")
        return n

    # ── Export ────────────────────────────────────────────────────────

    def to_text(self) -> str:
        """Render the buffer as a plain-text log suitable for download."""
        with self._lock:
            items = list(self._entries)
        out = io.StringIO()
        out.write("BT Gateway connection log\n")
        out.write(f"Exported: {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n")
        out.write(f"Entries: {len(items)}\n")
        out.write("-" * 72 + "\n")
        for e in items:
            line = f"{e['ts']}  {e['level'].upper():5s}  {e['step']:<24s}"
            if e.get("address"):
                line += f"  {e['address']}"
            if e.get("channel") is not None:
                line += f"  ch={e['channel']}"
            line += f"  {e['detail']}"
            if e.get("extras"):
                extras = " ".join(f"{k}={v}" for k, v in e["extras"].items())
                line += f"  [{extras}]"
            out.write(line + "\n")
        return out.getvalue()
