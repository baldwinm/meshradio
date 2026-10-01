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

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import aiosqlite

from . import migrations

log = logging.getLogger(__name__)


# The task that currently holds ``Database.transaction()``. A write method
# called from inside that task joins the open transaction instead of trying to
# take the lock again (which would deadlock); any other task waits its turn.
_TXN_OWNER: ContextVar[asyncio.Task | None] = ContextVar("meshradio_txn_owner", default=None)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _next_second(ts: str) -> str:
    """The second after ``ts``. Our timestamps are second-resolution, so this
    is how a row is nudged strictly past a cursor sitting on it."""
    try:
        dt = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return ts
    return (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def dedupe_hash(channel: str, sender: str, video_id: str, mesh_ts: float) -> str:
    bucket = int(mesh_ts // 60)
    raw = f"{channel}|{sender}|{video_id}|{bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()


class Core:
    """One connection, many coroutines.

    Every coroutine in the process shares this aiosqlite connection, and each
    ``await`` inside a write is a point where another coroutine's statements
    run on it. With the driver's implicit transactions that meant one task's
    rollback could discard another task's uncommitted UPDATE (it did: an
    ``add_track`` dedupe rollback once un-readied a track the cacher had just
    finished). So the connection runs in autocommit mode and every write goes
    through ``transaction()``, which serialises writers behind a lock and
    makes BEGIN/COMMIT explicit. Reads never wait.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._db: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: no implicit BEGIN before DML. A statement
        # outside transaction() is atomic on its own; a group is explicit.
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA foreign_keys=ON")
        # Ingest bursts, cacher sweeps, and session flushes all write through
        # this one connection; wait out short lock contention instead of
        # surfacing SQLITE_BUSY, and let WAL fsync lazily.
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.execute("PRAGMA synchronous=NORMAL")
        # Sized for an archive of tens of thousands of rows on a Pi: 32 MiB
        # of page cache (there is one connection), memory-mapped reads up to
        # 256 MiB of address space (a page read skips the copy into the
        # cache), and temp tables for sorts and GROUP BYs in memory.
        await self._db.execute("PRAGMA cache_size=-32000")
        await self._db.execute("PRAGMA mmap_size=268435456")
        await self._db.execute("PRAGMA temp_store=MEMORY")
        await self._migrate()

    async def close(self) -> None:
        if self._db:
            try:
                # Refresh the planner's statistics where the session's queries
                # showed they'd help — the recommended last act before close.
                await self._db.execute("PRAGMA optimize")
            except Exception:
                log.debug("PRAGMA optimize failed on close", exc_info=True)
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        assert self._db is not None, "Database.connect() not called"
        return self._db

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[None]:
        """Run a group of writes as one transaction, alone among writers.

        ``BEGIN IMMEDIATE`` on entry, ``COMMIT`` on a clean exit, ``ROLLBACK``
        on an exception (which is re-raised). Nested use from the same task
        joins the open transaction, so a batch can wrap the ordinary write
        methods: ``async with db.transaction(): await handle_message(...)``
        commits a whole relay push once instead of once per row. A failed
        statement inside the group undoes only itself (SQLite's default
        ABORT), so a caller that catches its error can carry on.

        Only the task that entered is "inside": a task spawned from within
        waits for the lock like any other, rather than writing into a
        transaction it doesn't own."""
        if _TXN_OWNER.get() is asyncio.current_task():
            yield                      # nested: the outer block commits
            return
        async with self._write_lock:
            token = _TXN_OWNER.set(asyncio.current_task())
            try:
                await self.db.execute("BEGIN IMMEDIATE")
                try:
                    yield
                except BaseException:
                    await self.db.execute("ROLLBACK")
                    raise
                await self.db.execute("COMMIT")
            finally:
                _TXN_OWNER.reset(token)

    async def _migrate(self) -> None:
        cur = await self.db.execute("PRAGMA user_version")
        row = await cur.fetchone()
        assert row is not None
        (version,) = row
        pending = migrations.MIGRATIONS[version:]
        if not pending:
            return
        # Each script runs as one transaction with its version bump inside,
        # so a failure part-way leaves the archive exactly as it was — not
        # the half-migrated state that executescript's statement-by-statement
        # autocommit left behind, with the version unbumped, for the next boot
        # to run into again from the top.
        #
        # Foreign keys are off for the run and checked after each script, the
        # SQLite documentation's own procedure for schema changes: the scripts
        # that rebuild tracks drop a table plays references, which enforcement
        # inside a transaction refuses, and a PRAGMA foreign_keys inside a
        # transaction is a no-op in any case (the scripts' own only ever
        # worked because they ran in autocommit).
        await self.db.execute("PRAGMA foreign_keys=OFF")
        try:
            for i, script in enumerate(pending, start=version + 1):
                try:
                    await self.db.executescript(
                        f"BEGIN;\n{script}\nPRAGMA user_version={i};\nCOMMIT;"
                    )
                except BaseException:
                    if self.db.in_transaction:
                        await self.db.execute("ROLLBACK")
                    raise
                cur = await self.db.execute("PRAGMA foreign_key_check")
                orphans = list(await cur.fetchall())
                if orphans:
                    log.warning(
                        "migration v%d left %d row(s) referencing a missing parent",
                        i, len(orphans),
                    )
        finally:
            await self.db.execute("PRAGMA foreign_keys=ON")

    # -- settings ----------------------------------------------------------

    async def get_setting(self, key: str, default: str | None = None) -> str | None:
        cur = await self.db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default

    async def set_setting(self, key: str, value: str) -> None:
        async with self.transaction():
            await self.db.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
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
