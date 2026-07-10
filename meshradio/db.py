"""aiosqlite persistence layer.

Schema (architecture §5): themes / tracks / plays / settings. All access from
other modules goes through the Database class — no raw SQL elsewhere.

Dedupe happens on two levels. ``dedupe_hash = sha256(channel + sender +
video_id + mesh_ts bucketed to 60s)`` (UNIQUE) lets mesh and CoreScope
ingestion coexist: the *same message* arriving via both paths inserts once.
Separately, a song is allowed only once per playlist — a repost of a video
already under a theme is dropped so it can't list twice (enforced in
``add_track`` and backed by a partial unique index on ``(theme_id, video_id)``).
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

log = logging.getLogger(__name__)

# A YouTube video id is exactly 11 chars of this set. A video id becomes both a
# cache filename and a yt-dlp CLI argument, so enforcing the shape here — at the
# one place tracks are inserted — keeps anything path-traversal- or
# argument-injection-shaped out of those sinks regardless of the ingest source.
_VIDEO_ID_RE = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")

MIGRATIONS: list[str] = [
    # v1 — initial schema
    """
    CREATE TABLE themes(
        id          INTEGER PRIMARY KEY,
        date        TEXT NOT NULL,               -- YYYY-MM-DD, channel-local (America/Chicago)
        title       TEXT NOT NULL,
        set_by      TEXT,
        raw_message TEXT,
        created_at  TEXT NOT NULL,
        UNIQUE(date, title)
    );
    CREATE TABLE tracks(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,                       -- unix seconds, message time on the mesh
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    CREATE TABLE plays(
        id        INTEGER PRIMARY KEY,
        track_id  INTEGER NOT NULL REFERENCES tracks(id),
        played_at TEXT NOT NULL,
        output    TEXT,
        completed INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX idx_plays_track ON plays(track_id);
    CREATE TABLE settings(
        key   TEXT PRIMARY KEY,
        value TEXT
    );
    """,
    # v2 — 'radio' track source (YouTube Mix continuations; not channel posts,
    # theme_id stays NULL so they never appear in the archive). SQLite can't
    # alter a CHECK, so rebuild the table.
    """
    PRAGMA foreign_keys=OFF;
    CREATE TABLE tracks_v2(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope','radio')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    INSERT INTO tracks_v2 SELECT * FROM tracks;
    DROP TABLE tracks;
    ALTER TABLE tracks_v2 RENAME TO tracks;
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    PRAGMA foreign_keys=ON;
    """,
    # v3 — per-visitor web session snapshots (embed hosting): survive deploys.
    """
    CREATE TABLE web_sessions(
        sid        TEXT PRIMARY KEY,
        updated_at TEXT NOT NULL,
        state      TEXT NOT NULL              -- JSON snapshot
    );
    """,
    # v4 — one playlist per day: lockable themes + merge existing duplicates.
    #
    # A theme *is* the day's playlist. Because UNIQUE is on (date, title), a
    # second "Theme: …" message with a different title used to insert a rival
    # theme row for the same date — splitting the day's tracks across two
    # playlists and "resetting" which theme new links attached to. Themes now
    # carry a `locked` flag: once a real theme is set for the day it is locked,
    # and later theme messages are ignored (enforced in IngestService).
    #
    # Backfill for days that already split: reassign every track to its day's
    # canonical theme (prefer a real title over an "Untitled —" placeholder,
    # then the earliest row), drop the now-empty rivals, and lock every
    # surviving real theme so it can't be reset either.
    """
    ALTER TABLE themes ADD COLUMN locked INTEGER NOT NULL DEFAULT 0;

    UPDATE tracks
    SET theme_id = (
        SELECT t.id FROM themes t
        WHERE t.date = (SELECT d.date FROM themes d WHERE d.id = tracks.theme_id)
        ORDER BY (t.title LIKE 'Untitled — %') ASC, t.id ASC
        LIMIT 1
    )
    WHERE theme_id IS NOT NULL;

    DELETE FROM themes
    WHERE id NOT IN (
        SELECT (
            SELECT t2.id FROM themes t2
            WHERE t2.date = dates.date
            ORDER BY (t2.title LIKE 'Untitled — %') ASC, t2.id ASC
            LIMIT 1
        )
        FROM (SELECT DISTINCT date FROM themes) dates
    );

    UPDATE themes SET locked = 1 WHERE title NOT LIKE 'Untitled — %';
    """,
    # v5 — 'letsmesh' track source: the backup analyzer feed
    # (analyzer.letsmesh.net), polled with the same CoreScope-compatible
    # adapter. SQLite can't alter a CHECK, so rebuild the table (as v2 did for
    # 'radio').
    """
    PRAGMA foreign_keys=OFF;
    CREATE TABLE tracks_v5(
        id           INTEGER PRIMARY KEY,
        video_id     TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT,
        artist       TEXT,
        duration     REAL,
        theme_id     INTEGER REFERENCES themes(id),
        sender       TEXT,
        mesh_ts      REAL,
        ingested_at  TEXT NOT NULL,
        source       TEXT NOT NULL CHECK(source IN ('mesh','corescope','radio','letsmesh')),
        cache_path   TEXT,
        cache_status TEXT NOT NULL DEFAULT 'pending'
                     CHECK(cache_status IN ('pending','ready','failed')),
        dedupe_hash  TEXT NOT NULL UNIQUE
    );
    INSERT INTO tracks_v5 SELECT * FROM tracks;
    DROP TABLE tracks;
    ALTER TABLE tracks_v5 RENAME TO tracks;
    CREATE INDEX idx_tracks_theme ON tracks(theme_id);
    CREATE INDEX idx_tracks_status ON tracks(cache_status);
    PRAGMA foreign_keys=ON;
    """,
    # v6 — one song per playlist. A song reposted to the same day used to
    # insert a second track row (dedupe_hash only catches the *same message*
    # arriving twice), so the playlist listed it twice. add_track now refuses a
    # video already present under a theme; collapse the dupes that already
    # accumulated and add a partial unique index as a hard backstop.
    #
    # Keep the earliest row per (theme_id, video_id) — by mesh time, then id —
    # repoint any plays at the survivor, then drop the rest. Radio filler
    # (theme_id NULL) is left alone: a mix legitimately echoes videos across
    # days, and SQLite's partial index treats NULL theme rows as distinct.
    """
    CREATE TEMP TABLE _keep AS
        SELECT t.id AS keep_id, t.theme_id AS theme_id, t.video_id AS video_id
        FROM tracks t
        WHERE t.theme_id IS NOT NULL
          AND t.id = (
              SELECT t2.id FROM tracks t2
              WHERE t2.theme_id = t.theme_id AND t2.video_id = t.video_id
              ORDER BY t2.mesh_ts, t2.id LIMIT 1
          );

    UPDATE plays SET track_id = (
        SELECT k.keep_id FROM tracks d JOIN _keep k
          ON k.theme_id = d.theme_id AND k.video_id = d.video_id
        WHERE d.id = plays.track_id
    )
    WHERE track_id IN (
        SELECT d.id FROM tracks d JOIN _keep k
          ON k.theme_id = d.theme_id AND k.video_id = d.video_id
        WHERE d.id <> k.keep_id
    );

    DELETE FROM tracks
    WHERE theme_id IS NOT NULL
      AND id NOT IN (SELECT keep_id FROM _keep);

    DROP TABLE _keep;

    CREATE UNIQUE INDEX idx_tracks_theme_video
        ON tracks(theme_id, video_id) WHERE theme_id IS NOT NULL;
    """,
    # v7 — index tracks.video_id. cached_track_for_video / tracks_for_video key
    # on it, and the cacher runs the reuse-existing-file check on every non-embed
    # download; without this it was a full table scan per download.
    """
    CREATE INDEX idx_tracks_video ON tracks(video_id);
    """,
]


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def dedupe_hash(channel: str, sender: str, video_id: str, mesh_ts: float) -> str:
    bucket = int(mesh_ts // 60)
    raw = f"{channel}|{sender}|{video_id}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Ingest bursts, cacher sweeps, and session flushes all write through
        # this one connection; wait out short lock contention instead of
        # surfacing SQLITE_BUSY, and let WAL fsync lazily.
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.connect() not called"
        return self._db

    async def _migrate(self) -> None:
        cur = await self.db.execute("PRAGMA user_version")
        (version,) = await cur.fetchone()
        for i, script in enumerate(MIGRATIONS[version:], start=version + 1):
            await self.db.executescript(script)
            await self.db.execute(f"PRAGMA user_version={i}")
            await self.db.commit()

    # -- settings ----------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        await self.db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await self.db.commit()

    # -- themes ------------------------------------------------------------

    async def create_theme(
        self,
        date: str,
        title: str,
        set_by: str | None = None,
        raw_message: str | None = None,
        locked: bool = False,
    ) -> dict[str, Any]:
        """Insert a theme; on (date, title) conflict return the existing row.

        ``locked`` marks the day's theme as final: the ingest pipeline refuses
        to reset a locked theme, so a later "Theme: …" message can't spawn a
        rival playlist. Auto-created "Untitled —" placeholders stay unlocked so
        the real theme can still adopt them (see ``adopt_theme``)."""
        # RETURNING (not lastrowid, which is unreliable after DO NOTHING)
        # distinguishes a fresh insert from a conflict no-op.
        cur = await self.db.execute(
            "INSERT INTO themes(date,title,set_by,raw_message,created_at,locked) "
            "VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(date,title) DO NOTHING RETURNING id",
            (date, title, set_by, raw_message, utcnow(), int(locked)),
        )
        inserted = await cur.fetchone()
        await self.db.commit()
        if inserted:
            row = await self._fetchone("SELECT * FROM themes WHERE id=?", (inserted["id"],))
        else:
            row = await self._fetchone(
                "SELECT * FROM themes WHERE date=? AND title=?", (date, title)
            )
        assert row is not None
        return row

    async def adopt_theme(
        self, theme_id: int, title: str, set_by: str | None = None, raw_message: str | None = None
    ) -> dict[str, Any]:
        """Give an existing (unlocked) theme a real title and lock it.

        Used when links arrived before the theme was announced: the day's
        "Untitled —" placeholder is renamed in place so every early track stays
        in the one playlist instead of being stranded on a separate theme."""
        await self.db.execute(
            "UPDATE themes SET title=?, set_by=?, raw_message=?, locked=1 WHERE id=?",
            (title, set_by, raw_message, theme_id),
        )
        await self.db.commit()
        row = await self._fetchone("SELECT * FROM themes WHERE id=?", (theme_id,))
        assert row is not None
        return row

    async def latest_theme_for_date(self, date: str) -> dict[str, Any] | None:
        return await self._fetchone(
            "SELECT * FROM themes WHERE date=? ORDER BY created_at DESC, id DESC LIMIT 1",
            (date,),
        )

    async def theme_by_id(self, theme_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM themes WHERE id=?", (theme_id,))

    # -- tracks ------------------------------------------------------------

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
        if not _VIDEO_ID_RE.match(video_id):
            log.warning("rejecting track with malformed video_id %r", video_id)
            return None
        if theme_id is not None:
            already = await self._fetchone(
                "SELECT 1 FROM tracks WHERE theme_id=? AND video_id=? LIMIT 1",
                (theme_id, video_id),
            )
            if already is not None:
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
            await self.db.commit()
        except aiosqlite.IntegrityError:
            # The one-song-per-playlist index caught a repost that slipped past
            # the check above (two ingest paths racing between our SELECT and
            # INSERT). The playlist already has it; treat as a dedupe no-op.
            await self.db.rollback()
            return None
        if inserted is None:
            return None
        return await self.track_by_id(inserted["id"])

    async def track_by_id(self, track_id: int) -> dict[str, Any] | None:
        return await self._fetchone("SELECT * FROM tracks WHERE id=?", (track_id,))

    async def update_track_metadata(
        self,
        track_id: int,
        *,
        title: str | None = None,
        artist: str | None = None,
        duration: float | None = None,
    ) -> None:
        await self.db.execute(
            "UPDATE tracks SET title=COALESCE(?,title), artist=COALESCE(?,artist), "
            "duration=COALESCE(?,duration) WHERE id=?",
            (title, artist, duration, track_id),
        )
        await self.db.commit()

    async def set_cache_status(
        self, track_id: int, status: str, cache_path: str | None = None
    ) -> None:
        await self.db.execute(
            "UPDATE tracks SET cache_status=?, cache_path=? WHERE id=?",
            (status, cache_path, track_id),
        )
        await self.db.commit()

    async def pending_tracks(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM tracks WHERE cache_status='pending' ORDER BY ingested_at"
        )

    async def tracks_for_video(self, video_id: str) -> list[dict[str, Any]]:
        """Every row for a video regardless of status (reposts share an id)."""
        return await self._fetchall(
            "SELECT * FROM tracks WHERE video_id=?", (video_id,)
        )

    async def cached_track_for_video(self, video_id: str) -> dict[str, Any] | None:
        """Another track row with the same video already cached (same song reposted)."""
        return await self._fetchone(
            "SELECT * FROM tracks WHERE video_id=? AND cache_status='ready' "
            "AND cache_path IS NOT NULL LIMIT 1",
            (video_id,),
        )

    # -- archive queries ----------------------------------------------------

    async def archive_days(self) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT t.date AS date, COUNT(DISTINCT t.id) AS themes, COUNT(tr.id) AS tracks "
            "FROM themes t LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "GROUP BY t.date ORDER BY t.date DESC"
        )

    async def themes_for_day(self, date: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT t.*, COUNT(tr.id) AS track_count FROM themes t "
            "LEFT JOIN tracks tr ON tr.theme_id=t.id "
            "WHERE t.date=? GROUP BY t.id ORDER BY t.created_at",
            (date,),
        )

    async def tracks_for_theme(self, theme_id: int) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT * FROM tracks WHERE theme_id=? ORDER BY mesh_ts", (theme_id,)
        )

    async def tracks_for_day(self, date: str) -> list[dict[str, Any]]:
        return await self._fetchall(
            "SELECT tr.* FROM tracks tr JOIN themes t ON tr.theme_id=t.id "
            "WHERE t.date=? ORDER BY tr.mesh_ts",
            (date,),
        )

    # -- search & stats -------------------------------------------------------

    async def search_tracks(self, query: str, limit: int = 100) -> list[dict[str, Any]]:
        """Channel tracks whose title, artist, sharer, or theme matches
        ``query`` (case-insensitive substring), newest first."""
        like = f"%{query.strip()}%"
        return await self._fetchall(
            "SELECT tr.*, t.date AS date, t.title AS theme_title "
            "FROM tracks tr JOIN themes t ON tr.theme_id=t.id "
            "WHERE tr.source != 'radio' AND ("
            "  tr.title LIKE ? OR tr.artist LIKE ? OR tr.sender LIKE ? OR t.title LIKE ?) "
            "ORDER BY tr.mesh_ts DESC LIMIT ?",
            (like, like, like, like, limit),
        )

    async def overall_stats(self) -> dict[str, Any]:
        row = await self._fetchone(
            "SELECT "
            " (SELECT COUNT(*) FROM tracks WHERE source!='radio') AS shares,"
            " (SELECT COUNT(DISTINCT video_id) FROM tracks WHERE source!='radio') AS songs,"
            " (SELECT COUNT(DISTINCT sender) FROM tracks WHERE source!='radio') AS sharers,"
            " (SELECT COUNT(*) FROM themes) AS themes,"
            " (SELECT COUNT(DISTINCT date) FROM themes) AS days"
        )
        return row or {}

    async def top_songs(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most-shared songs. A song is one row per day it was posted (same-day
        reposts collapse into one), so this counts distinct days it charted."""
        return await self._fetchall(
            "SELECT video_id, COALESCE(MAX(title), video_id) AS title, MAX(artist) AS artist,"
            " COUNT(*) AS shares, COUNT(DISTINCT sender) AS sharers "
            "FROM tracks WHERE source!='radio' "
            "GROUP BY video_id ORDER BY shares DESC, sharers DESC, title LIMIT ?",
            (limit,),
        )

    async def top_sharers(self, limit: int = 15) -> list[dict[str, Any]]:
        """Most active members by tracks posted."""
        return await self._fetchall(
            "SELECT sender, COUNT(*) AS shares, COUNT(DISTINCT video_id) AS songs "
            "FROM tracks WHERE source!='radio' AND sender IS NOT NULL AND sender!='' "
            "GROUP BY sender ORDER BY shares DESC, songs DESC LIMIT ?",
            (limit,),
        )

    async def busiest_themes(self, limit: int = 10) -> list[dict[str, Any]]:
        """Themes that drew the most songs."""
        return await self._fetchall(
            "SELECT t.date, t.title, COUNT(tr.id) AS tracks FROM themes t "
            "JOIN tracks tr ON tr.theme_id=t.id AND tr.source!='radio' "
            "GROUP BY t.id ORDER BY tracks DESC, t.date DESC LIMIT ?",
            (limit,),
        )

    # -- relay ----------------------------------------------------------------

    async def themes_since(self, ts: str, last_id: int = 0) -> list[dict[str, Any]]:
        """Themes created after the (timestamp, id) cursor, oldest first.
        The id tiebreaker means rows sharing the cursor's second (timestamps
        are second-resolution) are neither skipped nor re-sent forever."""
        return await self._fetchall(
            "SELECT * FROM themes WHERE created_at > ? "
            "OR (created_at = ? AND id > ?) ORDER BY created_at, id",
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
        """How many channel (non-radio) tracks this node knows. The relay
        compares counts to detect a wiped receiver and re-backfill."""
        row = await self._fetchone(
            "SELECT COUNT(*) AS n FROM tracks WHERE source != 'radio'"
        )
        return int(row["n"]) if row else 0

    # -- web sessions ----------------------------------------------------------

    async def save_web_session(self, sid: str, state: str) -> None:
        await self.db.execute(
            "INSERT INTO web_sessions(sid, updated_at, state) VALUES(?,?,?) "
            "ON CONFLICT(sid) DO UPDATE SET updated_at=excluded.updated_at, "
            "state=excluded.state",
            (sid, utcnow(), state),
        )
        await self.db.commit()

    async def load_web_session(self, sid: str) -> str | None:
        row = await self._fetchone(
            "SELECT state FROM web_sessions WHERE sid=?", (sid,)
        )
        return row["state"] if row else None

    async def delete_web_sessions(self, sids: list[str] | None = None, older_than: str | None = None) -> None:
        if sids:
            await self.db.executemany(
                "DELETE FROM web_sessions WHERE sid=?", [(s,) for s in sids]
            )
        if older_than:
            await self.db.execute(
                "DELETE FROM web_sessions WHERE updated_at < ?", (older_than,)
            )
        await self.db.commit()

    # -- plays / LRU ---------------------------------------------------------

    async def record_play(self, track_id: int, output: str | None) -> int:
        cur = await self.db.execute(
            "INSERT INTO plays(track_id,played_at,output) VALUES(?,?,?)",
            (track_id, utcnow(), output),
        )
        await self.db.commit()
        assert cur.lastrowid is not None
        return cur.lastrowid

    async def mark_play_completed(self, play_id: int) -> None:
        await self.db.execute("UPDATE plays SET completed=1 WHERE id=?", (play_id,))
        await self.db.commit()

    async def cached_tracks_lru(self) -> list[dict[str, Any]]:
        """Cached tracks, least-recently-played (then oldest-ingested) first."""
        return await self._fetchall(
            "SELECT tr.*, MAX(p.played_at) AS last_played FROM tracks tr "
            "LEFT JOIN plays p ON p.track_id=tr.id "
            "WHERE tr.cache_status='ready' AND tr.cache_path IS NOT NULL "
            "GROUP BY tr.id "
            "ORDER BY (last_played IS NOT NULL), last_played, tr.ingested_at"
        )

    # -- helpers -------------------------------------------------------------

    async def _fetchone(self, sql: str, params: tuple = ()) -> dict[str, Any] | None:
        cur = await self.db.execute(sql, params)
        row = await cur.fetchone()
        return dict(row) if row else None

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        cur = await self.db.execute(sql, params)
        rows = await cur.fetchall()
        return [dict(r) for r in rows]
