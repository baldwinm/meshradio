"""What the admin page (web/routes_admin.py) reads and writes.

Kept apart from the archive's own queries because every write here is an
operator's hand edit: each one is logged in ``admin_log`` with what it
changed, and the ones that can be undone carry enough to undo them.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import aiosqlite

from ..ingest.parse import untitled_theme
from .core import utcnow
from .fields import MAX_TITLE, clean_text
from .tracks import TrackQueries

# How long the activity log keeps an entry. Pruned as entries are written.
LOG_RETENTION_DAYS = 365

# Log entries by kind, for the activity log's filter.
SIGN_IN_ACTIONS = ("sign_in", "sign_in_failed", "sign_out")


class AdminQueries(TrackQueries):
    """The activity log, admin sign-ins, removals and their undo, hand edits
    of song details, and artist merges."""

    # -- activity log ---------------------------------------------------------

    async def log_admin(
        self,
        action: str,
        *,
        actor: str = "admin",
        ip: str | None = None,
        target: str | None = None,
        before: str | None = None,
        after: str | None = None,
        undo: dict[str, Any] | None = None,
    ) -> int:
        """Append one entry and return its id. ``undo`` is what
        ``routes_admin`` needs to reverse the change; None when it can't be."""
        cutoff = (datetime.now(UTC) - timedelta(days=LOG_RETENTION_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        async with self.transaction():
            cur = await self.db.execute(
                "INSERT INTO admin_log(at,actor,ip,action,target,before,after,undo) "
                "VALUES(?,?,?,?,?,?,?,?) RETURNING id",
                (utcnow(), actor, ip, action, target, before, after,
                 json.dumps(undo) if undo is not None else None),
            )
            row = await cur.fetchone()
            await self.db.execute("DELETE FROM admin_log WHERE at < ?", (cutoff,))
        assert row is not None
        return int(row["id"])

    async def admin_log_entries(
        self, kind: str = "all", limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest first. ``kind`` is all, changes or sign-ins."""
        clauses: list[str] = []
        params: list[Any] = []
        marks = ",".join("?" * len(SIGN_IN_ACTIONS))
        if kind == "changes":
            clauses.append(f"action NOT IN ({marks})")
            params.extend(SIGN_IN_ACTIONS)
        elif kind == "sign-ins":
            clauses.append(f"action IN ({marks})")
            params.extend(SIGN_IN_ACTIONS)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        params.append(limit)
        return await self._fetchall(
            f"SELECT * FROM admin_log {where}ORDER BY id DESC LIMIT ?", tuple(params)
        )

    async def admin_log_entry(self, entry_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM admin_log WHERE id=?", (entry_id,))

    async def mark_undone(self, entry_id: int, by_id: int) -> None:
        async with self.transaction():
            await self.db.execute(
                "UPDATE admin_log SET undone_by=? WHERE id=?", (by_id, entry_id)
            )

    # -- admin sign-ins -------------------------------------------------------

    async def create_admin_session(self, token_hash: str, ip: str | None, key_fp: str) -> None:
        now = time.time()
        async with self.transaction():
            await self.db.execute(
                "INSERT INTO admin_sessions(token_hash,created_at,seen_at,ip,key_fp) "
                "VALUES(?,?,?,?,?)",
                (token_hash, now, now, ip, key_fp),
            )

    async def admin_session(self, token_hash: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM admin_sessions WHERE token_hash=?", (token_hash,)
        )

    async def touch_admin_session(self, token_hash: str) -> None:
        async with self.transaction():
            await self.db.execute(
                "UPDATE admin_sessions SET seen_at=? WHERE token_hash=?",
                (time.time(), token_hash),
            )

    async def end_admin_session(self, token_hash: str) -> None:
        async with self.transaction():
            await self.db.execute("DELETE FROM admin_sessions WHERE token_hash=?", (token_hash,))

    async def prune_admin_sessions(self, oldest_created: float, oldest_seen: float) -> None:
        async with self.transaction():
            await self.db.execute(
                "DELETE FROM admin_sessions WHERE created_at < ? OR seen_at < ?",
                (oldest_created, oldest_seen),
            )

    # -- overview -------------------------------------------------------------

    async def untitled_days(self) -> list[dict[str, Any]]:
        """Days still on their ``Untitled —`` placeholder that have songs —
        the ones a visitor would actually see unnamed. Newest first."""
        return await self._fetchall(
            "SELECT t.date, t.title, COUNT(tr.id) AS tracks FROM themes t "
            "JOIN tracks tr ON tr.theme_id=t.id "
            "WHERE t.title LIKE 'Untitled — %' GROUP BY t.id ORDER BY t.date DESC"
        )

    async def newest_channel_tracks(self, limit: int = 5) -> list[dict[str, Any]]:
        """The songs that arrived most recently, with their day."""
        return await self._fetchall(
            "SELECT tr.*, t.date FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.source != 'radio' ORDER BY tr.ingested_at DESC, tr.id DESC LIMIT ?",
            (limit,),
        )

    async def cache_status_counts(self) -> dict[str, int]:
        """Channel songs by download state, for the device panel."""
        rows = await self._fetchall(
            "SELECT cache_status, COUNT(*) AS n FROM tracks "
            "WHERE source != 'radio' GROUP BY cache_status"
        )
        return {r["cache_status"] or "pending": r["n"] for r in rows}

    async def play_count(self, track_id: int) -> int:
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM plays WHERE track_id=?", (track_id,)
        )
        return int(row["n"]) if row else 0

    async def failed_tracks(self, limit: int = 50) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT tr.*, t.date FROM tracks tr JOIN themes t ON t.id=tr.theme_id "
            "WHERE tr.source != 'radio' AND tr.cache_status='failed' "
            "ORDER BY tr.ingested_at DESC LIMIT ?",
            (limit,),
        )

    # -- removals -------------------------------------------------------------

    async def removed_tracks(self) -> list[dict[str, Any]]:
        """Every removal still in force, newest first."""
        return await self._fetchall(
            "SELECT * FROM deleted_tracks ORDER BY deleted_at DESC, id DESC"
        )

    async def removed_track(self, date: str, video_id: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM deleted_tracks WHERE date=? AND video_id=?", (date, video_id)
        )

    async def restore_deleted_track(self, date: str, video_id: str) -> dict[str, Any] | None:
        """Lift a removal. With the removed row on file (any removal made
        since the admin page landed) the song goes straight back on its day,
        title and all; its plays are gone for good. Without one, only the
        block is lifted, and the song returns when the channel history is
        next read in full. Returns the restored track, or None if only the
        block went (or nothing was removed)."""
        async with self.transaction():
            tomb = await self.removed_track(date, video_id)
            if tomb is None:
                return None
            await self.db.execute("DELETE FROM deleted_tracks WHERE id=?", (tomb["id"],))
            if not tomb.get("track_json"):
                return None
            old = json.loads(tomb["track_json"])
            theme = await self.theme_by_id(old["theme_id"]) if old.get("theme_id") else None
            if theme is None or theme["date"] != date:
                theme = await self.latest_theme_for_date(date)
            if theme is None:
                theme = await self.create_theme(date, untitled_theme(date))
            try:
                cur = await self.db.execute(
                    "INSERT INTO tracks(video_id,url,title,artist,duration,theme_id,sender,"
                    "mesh_ts,ingested_at,source,dedupe_hash,meta_edited_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(dedupe_hash) DO NOTHING RETURNING id",
                    (old["video_id"], old["url"], old.get("title"), old.get("artist"),
                     old.get("duration"), theme["id"], old.get("sender") or "",
                     old["mesh_ts"], utcnow(), old["source"], old["dedupe_hash"],
                     old.get("meta_edited_at")),
                )
                inserted = await cur.fetchone()
            except aiosqlite.IntegrityError:
                return None        # the day already has it again
            if inserted is None:
                return None
            return await self.track_by_id(inserted["id"])

    # -- song details ---------------------------------------------------------

    async def edit_track(
        self, track_id: int, title: str, artist: str | None
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Set a song's title and artist by hand, and pin them so automatic
        metadata can't replace them. Returns the row before and after.
        Raises ValueError for an empty title or an unknown track."""
        clean_title = clean_text(title, MAX_TITLE)
        if clean_title is None:
            raise ValueError("a song needs a title")
        clean_artist = clean_text(artist, MAX_TITLE)
        async with self.transaction():
            before = await self.track_by_id(track_id)
            if before is None:
                raise ValueError("no such song")
            await self.db.execute(
                "UPDATE tracks SET title=?, artist=?, meta_edited_at=? WHERE id=?",
                (clean_title, clean_artist, utcnow(), track_id),
            )
            after = await self.track_by_id(track_id)
        assert after is not None
        return before, after

    async def revert_track_edit(
        self, track_id: int, title: str | None, artist: str | None, edited_at: str | None
    ) -> bool:
        """Put a song's title, artist and pin back exactly as they were."""
        async with self.transaction():
            cur = await self.db.execute(
                "UPDATE tracks SET title=?, artist=?, meta_edited_at=? WHERE id=?",
                (title, artist, edited_at, track_id),
            )
        return cur.rowcount > 0

    # -- artists --------------------------------------------------------------

    async def artist_spellings(self) -> list[dict[str, Any]]:
        """Every artist string channel songs carry, with its song count."""
        return await self._fetchall(
            "SELECT artist, COUNT(*) AS songs FROM tracks "
            "WHERE source != 'radio' AND artist IS NOT NULL AND artist != '' "
            "GROUP BY artist ORDER BY songs DESC, artist"
        )

    async def artist_aliases(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM artist_aliases ORDER BY canonical COLLATE NOCASE, alias"
        )

    async def merge_artists(
        self, spellings: list[str], canonical: str
    ) -> list[tuple[int, str]]:
        """Fold ``spellings`` into ``canonical``: every song carrying one is
        respelled, and a song arriving later with one is respelled as it's
        stored (``canonical_artist``). Returns ``(track_id, old artist)`` for
        each song changed, which is what undoing it needs."""
        name = clean_text(canonical, MAX_TITLE)
        if name is None:
            raise ValueError("an artist needs a name")
        changed: list[tuple[int, str]] = []
        async with self.transaction():
            # The new name mustn't itself map elsewhere, or lookups would
            # chase it; and earlier merges into a spelling now folded follow.
            await self.db.execute(
                "DELETE FROM artist_aliases WHERE alias = ? COLLATE BINARY AND canonical != ?",
                (name, name),
            )
            for spelling in spellings:
                if spelling == name:
                    continue
                await self.db.execute(
                    "INSERT INTO artist_aliases(alias,canonical,created_at) VALUES(?,?,?) "
                    "ON CONFLICT(alias) DO UPDATE SET canonical=excluded.canonical",
                    (spelling, name, utcnow()),
                )
                await self.db.execute(
                    "UPDATE artist_aliases SET canonical=? WHERE canonical = ? COLLATE BINARY",
                    (name, spelling),
                )
                rows = await self._fetchall(
                    "SELECT id, artist FROM tracks WHERE artist = ? COLLATE BINARY",
                    (spelling,),
                )
                if rows:
                    await self.db.execute(
                        "UPDATE tracks SET artist=? WHERE artist = ? COLLATE BINARY",
                        (name, spelling),
                    )
                changed += [(r["id"], r["artist"]) for r in rows]
        return changed

    async def unmerge_artists(
        self, spellings: list[str], canonical: str, changed: list[tuple[int, str]]
    ) -> None:
        """Undo ``merge_artists``: drop the aliases and give each song it
        respelled its old spelling back, unless it has been changed since."""
        async with self.transaction():
            for spelling in spellings:
                await self.db.execute(
                    "DELETE FROM artist_aliases WHERE alias = ? COLLATE BINARY "
                    "AND canonical = ?",
                    (spelling, canonical),
                )
            for track_id, old in changed:
                await self.db.execute(
                    "UPDATE tracks SET artist=? WHERE id=? AND artist = ? COLLATE BINARY",
                    (old, track_id, canonical),
                )
