"""aiosqlite persistence layer — the ``Database`` facade.

Everything else in meshradio talks to one ``Database`` object; its methods
live in this package by concern (themes, tracks, the archive's read side,
artists and weeks, the relay's cursors, visitor sessions, the admin page's
edits) on top of ``core.Core``, which owns the connection, the write
transaction and the migrations. No raw SQL outside this package.
"""

from __future__ import annotations

from . import migrations
from .admin import AdminQueries
from .archive import FTS_MIN_CHARS, ArchiveQueries
from .browse import BrowseQueries
from .core import Core, dedupe_hash, utcnow
from .fields import (
    MAX_DURATION_S,
    MAX_SENDER,
    MAX_TITLE,
    VIDEO_ID_RE,
    clean_duration,
    clean_text,
)
from .migrations import MIGRATIONS
from .relay import RelayQueries
from .themes import ThemeQueries
from .tracks import TrackQueries
from .web_sessions import WebSessionQueries


class Database(
    AdminQueries, ArchiveQueries, BrowseQueries, RelayQueries, WebSessionQueries
):
    """One connection, many coroutines — see ``core.Core`` for the model.
    ``AdminQueries`` brings ``TrackQueries`` and ``ThemeQueries`` with it."""


__all__ = [
    "AdminQueries",
    "ArchiveQueries",
    "BrowseQueries",
    "Core",
    "Database",
    "FTS_MIN_CHARS",
    "MAX_DURATION_S",
    "MAX_SENDER",
    "MAX_TITLE",
    "MIGRATIONS",
    "RelayQueries",
    "ThemeQueries",
    "TrackQueries",
    "VIDEO_ID_RE",
    "WebSessionQueries",
    "clean_duration",
    "clean_text",
    "dedupe_hash",
    "migrations",
    "utcnow",
]
