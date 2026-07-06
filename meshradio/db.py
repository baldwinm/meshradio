"""aiosqlite persistence layer.

Schema (architecture §5): themes / tracks / plays / settings. All access from
other modules goes through the Database class — no raw SQL elsewhere.

Dedupe: mesh and CoreScope ingestion coexist via
``dedupe_hash = sha256(channel + sender + video_id + mesh_ts bucketed to 60s)``
with a UNIQUE constraint; whichever path delivers first wins, the other no-ops.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

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
        self, date: str, title: str, set_by: str | None = None, raw_message: str | None = None
    ) -> dict[str, Any]:
        """Insert a theme; on (date, title) conflict return the existing row."""
        # RETURNING (not lastrowid, which is unreliable after DO NOTHING)
        # distinguishes a fresh insert from a conflict no-op.
        cur = await self.db.execute(
            "INSERT INTO themes(date,title,set_by,raw_message,created_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(date,title) DO NOTHING RETURNING id",
            (date, title, set_by, raw_message, utcnow()),
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
        """Insert a track. Returns the new row, or None if deduped."""
        dh = dedupe_hash(channel, sender, video_id, mesh_ts)
        cur = await self.db.execute(
            "INSERT INTO tracks(video_id,url,title,artist,theme_id,sender,mesh_ts,"
            "ingested_at,source,dedupe_hash) VALUES(?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(dedupe_hash) DO NOTHING RETURNING id",
            (video_id, url, title, artist, theme_id, sender, mesh_ts, utcnow(), source, dh),
        )
        inserted = await cur.fetchone()
        await self.db.commit()
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
