"""Bounds on what a row may hold, applied where rows are written."""

from __future__ import annotations

import math
import re
from typing import Any

# A YouTube video id is exactly 11 chars of this set. A video id becomes both a
# cache filename and a yt-dlp CLI argument, so enforcing the shape here — at the
# one place tracks are inserted — keeps anything path-traversal- or
# argument-injection-shaped out of those sinks regardless of the ingest source.
# The delete CLI checks its argument against the same rule, so an id that could
# never have been stored is rejected before it goes looking for one.
VIDEO_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")

# Free text reaches the archive from the mesh, two analyzers, the relay, yt-dlp
# and oEmbed, and from there it lands in every page, the feed, the WebSocket
# and the log. It is bounded here, where rows are written, so that no source
# has to be trusted to bound it: a title is a line, not a document, and a mesh
# node name is short. Control characters go — one in a title took the feed
# down once, and one in a sender name is a forged log line — and whitespace
# collapses to single spaces. Durations get the same treatment because
# ``inf`` and ``nan`` parse as floats, and either one in a shared row breaks
# every page and state read that formats it (the relay used to be able to
# plant one; the browser's own report was already refused at the route).
MAX_TITLE = 256          # track and theme titles, artists
MAX_SENDER = 64          # MeshCore node names are at most 32 characters
MAX_DURATION_S = 24 * 3600

# C0 and C1 controls (newlines and tabs included: these fields are one line),
# plus the two non-characters XML forbids, which the feed had to strip itself.
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f￾￿]")


def clean_text(value: Any, limit: int) -> str | None:
    """``value`` as one bounded line of text, or None when nothing is left."""
    if value is None:
        return None
    text = " ".join(_CONTROL.sub(" ", str(value)).split())
    return text[:limit].strip() or None


def clean_duration(value: Any) -> float | None:
    """A track length in seconds, or None for anything that isn't one."""
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(seconds) or not 0 < seconds <= MAX_DURATION_S:
        return None
    return seconds
