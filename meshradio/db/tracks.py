"""Track rows and the listening record."""

from __future__ import annotations

import json
import logging
from typing import Any

import aiosqlite

from .core import dedupe_hash, utcnow
from .fields import (
    MAX_SENDER,
    MAX_TITLE,
    VIDEO_ID_RE,
    clean_duration,
    clean_text,
)
from .themes import ThemeQueries

log = logging.getLogger(__name__)


class TrackQueries(ThemeQueries):
    """Tracks (inserts, metadata, cache state, deletion) and plays."""

    async def add_track(
        self,
        *,
        video_id: str,
        url: str,
        channel: str,
        sender: str,
        mesh_ts: float,
        source: str,
        theme_id: int | None,
        title: str | None = None,
        artist: str | None = None,
    ) -> dict[str, Any] | None:
        """Insert a track. Returns the new row, or None if deduped or if the
        video id is malformed (rejected before it can reach a cache path or a
        yt-dlp argument).

        Two dedupe rules apply. ``dedupe_hash`` (channel+sender+video+60s
        bucket) collapses the *same message* arriving via more than one ingest
        path. Separately, a song is only allowed once per playlist: if this
        video already sits under ``theme_id``, the repost is dropped so it
        can't show up twice in the day's list — no matter who reposts it or how
        much later. Radio filler (``theme_id`` NULL) is exempt; a mix can echo
        the same video across days."""
        if not VIDEO_ID_RE.match(video_id):
            log.warning("rejecting track with malformed video_id %r", video_id)
            return None
        # Bounded before the dedupe hash, so the same message hashes the same
        # whichever path (and whatever trailing junk) it arrived with.
        sender = clean_text(sender, MAX_SENDER) or ""
        title = clean_text(title, MAX_TITLE)
        artist = clean_text(artist, MAX_TITLE)
        # Check and insert under one transaction: the write lock means no other
        # ingest path can slip a repost in between them.
        async with self.transaction():
            artist = await self.canonical_artist(artist)
            if theme_id is not None:
                already = await self._fetchone(
                    "SELECT 1 FROM tracks WHERE theme_id=? AND video_id=? LIMIT 1",
                    (theme_id, video_id),
                )
                if already is not None:
                    return None
                if await self.is_deleted(theme_id, video_id):
                    # The operator removed this song from the day by hand. The
                    # message is still on the channel, so ingest keeps offering
                    # it back; the tombstone is what makes the removal stick.
                    log.info("skipping %s on theme %s: deleted by the operator", video_id, theme_id)
                    return None
            dh = dedupe_hash(channel, sender, video_id, mesh_ts)
            try:
                cur = await self.db.execute(
                    "INSERT INTO tracks(video_id,url,title,artist,theme_id,sender,mesh_ts,"
                    "ingested_at,source,dedupe_hash) VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(dedupe_hash) DO NOTHING RETURNING id",
                    (video_id, url, title, artist, theme_id, sender, mesh_ts, utcnow(), source, dh),
                )
                inserted = await cur.fetchone()
            except aiosqlite.IntegrityError:
                # The one-song-per-playlist index is the backstop for the check
                # above. A failed INSERT undoes only itself, so a batch this is
                # part of carries on; the playlist already has the song.
                return None
            if inserted is None:
                return None
            return await self.track_by_id(inserted["id"])

    async def track_by_id(self, track_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM tracks WHERE id=?", (track_id,))

    async def tracks_by_ids(
        self, ids: list[int] | tuple[int, ...], chunk: int = 500
    ) -> dict[int, dict[str, Any]]:
        """Rows for many track ids, keyed by id, in one round trip per
        ``chunk`` (SQLite caps bound parameters). Ids that no longer exist
        are simply absent. A session restore used to do one query per queued
        track — 200 round trips through the driver thread for a long day."""
        found: dict[int, dict[str, Any]] = {}
        wanted = list(dict.fromkeys(ids))
        for start in range(0, len(wanted), chunk):
            part = wanted[start:start + chunk]
            rows = await self._fetchall(
                f"SELECT * FROM tracks WHERE id IN ({','.join('?' * len(part))})", tuple(part)
            )
            found.update((row["id"], row) for row in rows)
        return found

    async def is_deleted(self, theme_id: int, video_id: str) -> bool:
        """Was this video removed by hand from the day ``theme_id`` belongs to?

        Keyed on the *day* rather than the theme row, so the tombstone still
        applies if the day's playlist is a different row than it was — a
        placeholder that got cleaned up, a receiver that rebuilt its own."""
        row = await self._fetchone(
            "SELECT 1 FROM deleted_tracks d JOIN themes t ON t.date=d.date "
            "WHERE t.id=? AND d.video_id=? LIMIT 1",
            (theme_id, video_id),
        )
        return row is not None

    async def delete_track(self, track_id: int) -> dict[str, Any] | None:
        """Remove one song from the archive for good. Returns the deleted row.

        The out-of-band fix for a song that shouldn't be in the day's playlist
        — posted before the theme was set, or posted to the wrong day. Ingest
        can't undo it: the link is still on the channel and a repost is a
        dedupe no-op, so the only way out is here.

        Three things happen together, and all three are needed for the removal
        to hold: the track row goes, its play history goes with it (``plays``
        references it), and a ``deleted_tracks`` tombstone records the day and
        video so a re-backfill can't quietly put it back. Radio filler (no
        theme) leaves no tombstone — it isn't on the channel to come back.

        The tombstone keeps the whole row too, so ``restore_deleted_track``
        can put the song back exactly as it was (its plays excepted).

        The cached audio file isn't touched here; the caller owns the disk."""
        async with self.transaction():
            track = await self.track_by_id(track_id)
            if track is None:
                return None
            date = None
            if track["theme_id"] is not None:
                theme = await self.theme_by_id(track["theme_id"])
                date = theme["date"] if theme else None
            await self.db.execute("DELETE FROM plays WHERE track_id=?", (track_id,))
            await self.db.execute("DELETE FROM tracks WHERE id=?", (track_id,))
            if date is not None:
                await self.db.execute(
                    "INSERT INTO deleted_tracks(date,video_id,title,sender,deleted_at,track_json) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(date,video_id) DO NOTHING",
                    (date, track["video_id"], track["title"], track["sender"], utcnow(),
                     json.dumps(track)),
                )
        return track

    async def delete_empty_placeholder(self, theme_id: int) -> bool:
        """Drop a day's ``Untitled — `` placeholder once it holds no songs.

        Only ever an auto-created, unlocked placeholder with an empty playlist:
        nobody named it and nothing is filed under it, so leaving it behind
        would light up a calendar tile for a day that has nothing to play. A
        real (locked, titled) theme stays even when emptied — somebody chose
        that title, and the day is still a day the channel named."""
        async with self.transaction():
            theme = await self.theme_by_id(theme_id)
            if theme is None or theme["locked"] or not theme["title"].startswith("Untitled — "):
                return False
            row = await self._fetchone(
                "SELECT COUNT(*) AS n FROM tracks WHERE theme_id=?", (theme_id,)
            )
            if row and row["n"]:
                return False
            await self.db.execute("DELETE FROM themes WHERE id=?", (theme_id,))
        return True

    async def update_track_metadata(
        self,
        track_id: int,
        *,
        title: str | None = None,
        artist: str | None = None,
        duration: float | None = None,
    ) -> None:
        """Fill or replace a track's metadata. A value that doesn't survive
        cleaning (an empty or control-only title, a non-finite length) is
        treated as not supplied, so it can never replace a good one."""
        title = clean_text(title, MAX_TITLE)
        artist = clean_text(artist, MAX_TITLE)
        duration = clean_duration(duration)
        async with self.transaction():
            artist = await self.canonical_artist(artist)
            # A title or artist the operator corrected by hand (the admin
            # page) outranks whatever oEmbed or a relay re-push says later.
            await self.db.execute(
                "UPDATE tracks SET "
                "title=CASE WHEN meta_edited_at IS NULL THEN COALESCE(?,title) ELSE title END, "
                "artist=CASE WHEN meta_edited_at IS NULL THEN COALESCE(?,artist) "
                "ELSE artist END, "
                "duration=COALESCE(?,duration) WHERE id=?",
                (title, artist, duration, track_id),
            )

    async def canonical_artist(self, artist: str | None) -> str | None:
        """``artist`` as the operator asked for it to be spelled: a spelling
        merged on the admin page's Artists screen maps to the name it was
        merged into, so a song arriving later joins the merged artist."""
        if not artist:
            return artist
        row = await self._fetchone(
            "SELECT canonical FROM artist_aliases WHERE alias=?", (artist,)
        )
        return row["canonical"] if row else artist

    async def fill_track_duration(self, track_id: int, seconds: float) -> bool:
        """Set a track's duration only if it has none. Returns whether it did.

        The browser-reported length (``/api/duration``) goes through here
        rather than ``update_track_metadata``: that report is unauthenticated
        and the row is shared, so it may complete a blank but never replace a
        value the archive already holds."""
        cleaned = clean_duration(seconds)
        if cleaned is None:
            return False
        seconds = cleaned
        async with self.transaction():
            cur = await self.db.execute(
                "UPDATE tracks SET duration=? WHERE id=? AND duration IS NULL",
                (seconds, track_id),
            )
        return cur.rowcount > 0

    async def set_cache_status(
        self, track_id: int, status: str, cache_path: str | None = None
    ) -> None:
        async with self.transaction():
            await self.db.execute(
                "UPDATE tracks SET cache_status=?, cache_path=? WHERE id=?",
                (status, cache_path, track_id),
            )

    async def pending_tracks(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM tracks WHERE cache_status='pending' ORDER BY ingested_at"
        )

    async def tracks_for_video(self, video_id: str) -> list[dict[str, Any]]:
        """Every row for a video regardless of status (reposts share an id)."""
        return await self._fetchall(
            "SELECT * FROM tracks WHERE video_id=?", (video_id,)
        )

    async def last_played_track(self) -> dict[str, Any] | None:
        """The most recently played track, if any (radio-mode seed fallback)."""
        return await self._fetchone(
            "SELECT tr.* FROM tracks tr JOIN plays p ON p.track_id=tr.id "
            "ORDER BY p.played_at DESC LIMIT 1"
        )

    async def cached_track_for_video(self, video_id: str) -> dict[str, Any] | None:
        """Another track row with the same video already cached (same song reposted)."""
        return await self._fetchone(
            "SELECT * FROM tracks WHERE video_id=? AND cache_status='ready' "
            "AND cache_path IS NOT NULL LIMIT 1",
            (video_id,),
        )


    # -- plays / LRU ---------------------------------------------------------

    async def record_play(self, track_id: int, output: str | None) -> int:
        """Log a play, and stamp the track with it: ``last_played_at`` is what
        the cache pruner orders by, so the pruner never has to aggregate
        ``plays`` to find the least-recently-played file."""
        now = utcnow()
        async with self.transaction():
            cur = await self.db.execute(
                "INSERT INTO plays(track_id,played_at,output) VALUES(?,?,?)",
                (track_id, now, output),
            )
            await self.db.execute(
                "UPDATE tracks SET last_played_at=? WHERE id=?", (now, track_id)
            )
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def mark_play_completed(self, play_id: int) -> None:
        async with self.transaction():
            await self.db.execute("UPDATE plays SET completed=1 WHERE id=?", (play_id,))

    async def cached_tracks_lru(self, limit: int = 100) -> list[dict[str, Any]]:
        """The ``limit`` cached tracks the pruner should drop first: never
        played (NULL sorts first), then least recently, then oldest-ingested.
        One walk down ``idx_tracks_lru`` — it used to be a GROUP BY over
        tracks×plays returning every cached row."""
        return await self._fetchall(
            "SELECT *, last_played_at AS last_played FROM tracks "
            "WHERE cache_status='ready' AND cache_path IS NOT NULL "
            "ORDER BY last_played_at, ingested_at LIMIT ?",
            (limit,),
        )
