"""What the relay pusher reads to mirror this node's history."""

from __future__ import annotations

import logging
from typing import Any

from .core import Core

log = logging.getLogger(__name__)


class RelayQueries(Core):
    """Cursor-driven reads for the relay, and the counts it compares."""

    async def themes_since(self, ts: str, last_id: int = 0) -> list[dict[str, Any]]:
        """Themes created *or adopted* after the (timestamp, id) cursor, oldest
        first, each carrying the ``relay_ts`` the cursor should record.

        Keying on updated_at-else-created_at is what lets an adopted
        placeholder relay: it was skipped as an untitled placeholder on an
        earlier push, and only the adoption moves it back in front of the
        cursor. The id tiebreaker means rows sharing the cursor's second
        (timestamps are second-resolution) are neither skipped nor re-sent
        forever."""
        return await self._fetchall(
            "SELECT *, COALESCE(updated_at, created_at) AS relay_ts FROM themes "
            "WHERE COALESCE(updated_at, created_at) > ? "
            "OR (COALESCE(updated_at, created_at) = ? AND id > ?) "
            "ORDER BY relay_ts, id",
            (ts, ts, last_id),
        )

    async def tracks_since(self, ts: str, last_id: int = 0) -> list[dict[str, Any]]:
        """Channel tracks ingested after the (timestamp, id) cursor, oldest
        first. Radio filler is excluded — it's local jukebox state, not
        channel history."""
        return await self._fetchall(
            "SELECT * FROM tracks WHERE source != 'radio' AND (ingested_at > ? "
            "OR (ingested_at = ? AND id > ?)) ORDER BY ingested_at, id",
            (ts, ts, last_id),
        )

    async def channel_track_count(self) -> int:
        """How many channel (non-radio) tracks this node holds right now."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM tracks WHERE source != 'radio'"
        )
        return int(row["n"]) if row else 0

    async def relay_track_total(self) -> int:
        """How many channel songs this node has *accounted for* — the ones it
        holds plus the ones an operator deleted.

        This, not ``channel_track_count``, is what the relay compares: a
        receiver behind on history is a wipe to re-backfill, but a receiver
        that is one song lighter because someone ran ``--delete-track`` on it
        is exactly right, and counting the tombstone keeps the pusher from
        re-pushing the whole channel every interval trying to restore a song
        the tombstone will reject anyway."""
        row = await self._fetchone(
            "SELECT (SELECT COUNT(*) FROM tracks WHERE source != 'radio') "
            "+ (SELECT COUNT(*) FROM deleted_tracks) AS n"
        )
        return int(row["n"]) if row else 0
