"""Template filters for the admin pages: times, sizes and addresses."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def fmt_ago(seconds: float | None) -> str:
    """``125`` -> ``2 min``: how long ago, to the unit that matters."""
    if seconds is None:
        return "never"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} days"


def fmt_when(value: Any, tz: Any) -> str:
    """An epoch or a stored ``…Z`` timestamp as ``6 Oct 12:40`` in the
    channel's time zone."""
    if value in (None, ""):
        return "—"
    try:
        if isinstance(value, int | float):
            moment = datetime.fromtimestamp(value, UTC)
        else:
            moment = datetime.strptime(str(value), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (ValueError, OverflowError, OSError):
        return str(value)
    local = moment.astimezone(tz)
    return f"{local.day} {local:%b %H:%M}"


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def short_ip(ip: str | None) -> str:
    """Enough of an address to tell yours from a stranger's on screen; the
    log keeps it whole."""
    if not ip:
        return "—"
    if ":" in ip:
        return ":".join(ip.split(":")[:2]) + ":…"
    parts = ip.split(".")
    return ".".join(parts[:2]) + ".x.x" if len(parts) == 4 else ip


FILTERS = {"ago": fmt_ago, "when": fmt_when, "bytes": fmt_bytes, "short_ip": short_ip}
