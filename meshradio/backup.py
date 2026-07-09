"""Rotating on-disk snapshots of the archive DB.

A safety net distinct from the host's disk durability: a point-in-time copy to
roll back to after a bad migration or corruption. Uses SQLite's online backup
API, so a snapshot is internally consistent even while the app is mid-write —
no need to pause ingestion. Snapshots land next to the DB (default
``<data_dir>/backups``) and the oldest are pruned to a fixed count.

For protection against losing the whole disk, pair this with host-level disk
snapshots (Render takes automatic daily ones on paid instances) or point
``[backup].dir`` at separate storage.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import BackupConfig
from .runtime import Service

log = logging.getLogger(__name__)

_PREFIX = "meshradio-"
_REQUIRED_TABLES = ("tracks", "themes")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def snapshot(db_path: str | Path, dest_dir: str | Path, label: str = "auto") -> Path | None:
    """Write a consistent copy of ``db_path`` into ``dest_dir``. Returns the
    snapshot path, or None if the source doesn't exist yet (first boot)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return None
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{_PREFIX}{_stamp()}-{label}.db"
    # Write to a sibling temp first, then atomically rename, so a crash
    # mid-backup never leaves a half-written file that rotation would keep.
    tmp = dest.with_name(dest.name + ".part")
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(tmp))
        try:
            src.backup(dst)          # online backup: page-by-page, writer-safe
            # The live DB is WAL; a copy of a WAL header makes every later open
            # spawn -wal/-shm sidecars next to the snapshot. Store snapshots in
            # rollback-journal mode so each is a clean, self-contained file.
            dst.execute("PRAGMA journal_mode=DELETE")
        finally:
            dst.close()
    finally:
        src.close()
    tmp.replace(dest)
    return dest


def rotate(dest_dir: str | Path, keep: int) -> None:
    """Keep the newest ``keep`` snapshots, delete the rest. Names embed a
    fixed-width UTC timestamp, so a lexical sort is chronological."""
    if keep <= 0:
        return
    snaps = sorted(Path(dest_dir).glob(f"{_PREFIX}*.db"))
    for old in snaps[:-keep]:
        try:
            old.unlink()
        except OSError:
            log.warning("couldn't prune old backup %s", old.name)


def list_snapshots(dest_dir: str | Path) -> list[Path]:
    """Snapshots in ``dest_dir``, oldest first (names sort chronologically)."""
    return sorted(Path(dest_dir).glob(f"{_PREFIX}*.db"))


def resolve_snapshot(dest_dir: str | Path, which: str) -> Path | None:
    """Pick a snapshot to restore. ``which`` is ``"latest"``, a bare filename
    in ``dest_dir``, or a full path. Returns None if nothing matches."""
    which = (which or "").strip()
    if which in ("", "latest"):
        snaps = list_snapshots(dest_dir)
        return snaps[-1] if snaps else None
    direct = Path(which)
    if direct.is_file():
        return direct
    named = Path(dest_dir) / which
    return named if named.is_file() else None


def is_archive_db(path: str | Path) -> bool:
    """True if ``path`` is an intact SQLite DB carrying the archive tables — a
    guard against restoring a truncated/foreign file over the live DB."""
    # immutable=1: read the file directly without locking or spawning -wal/-shm
    # sidecars next to a snapshot we're only inspecting.
    try:
        conn = sqlite3.connect(f"file:{Path(path)}?immutable=1", uri=True)
    except sqlite3.Error:
        return False
    try:
        if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            return False
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        return all(t in names for t in _REQUIRED_TABLES)
    except sqlite3.Error:
        return False
    finally:
        conn.close()


def restore(db_path: str | Path, snapshot_path: str | Path,
            backup_dir: str | Path | None = None) -> Path | None:
    """Replace the DB at ``db_path`` with ``snapshot_path``. Snapshots the
    current DB first (label ``prerestore``) when ``backup_dir`` is given, so a
    restore is itself reversible; returns that safety copy's path (or None).

    Run with the service stopped. Refuses a snapshot that isn't a valid archive
    DB, and clears the old WAL/SHM sidecars — they belong to the replaced file
    and would corrupt the restored one on next open."""
    db_path = Path(db_path)
    snapshot_path = Path(snapshot_path)
    if not snapshot_path.is_file():
        raise FileNotFoundError(snapshot_path)
    if not is_archive_db(snapshot_path):
        raise ValueError(f"{snapshot_path.name} is not a valid meshradio archive DB")

    safety = None
    if db_path.exists() and backup_dir is not None:
        safety = snapshot(db_path, backup_dir, label="prerestore")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = db_path.with_name(db_path.name + ".restore")
    shutil.copy2(snapshot_path, tmp)
    tmp.replace(db_path)
    for suffix in ("-wal", "-shm"):
        try:
            db_path.with_name(db_path.name + suffix).unlink()
        except FileNotFoundError:
            pass
    return safety


class BackupService(Service):
    def __init__(self, config: BackupConfig, db_path: str | Path, backup_dir: str | Path):
        self.config = config
        self.db_path = Path(db_path)
        self.backup_dir = Path(backup_dir)

    async def _run(self) -> None:
        # Snapshot immediately on boot, then on the configured interval.
        while True:
            await self.run_once()
            await asyncio.sleep(self.config.interval_s)

    async def run_once(self, label: str = "auto") -> Path | None:
        """Take one snapshot and prune. Runs the blocking sqlite work off the
        event loop; a failure is logged, never fatal (backups must not take the
        radio down)."""
        try:
            dest = await asyncio.to_thread(snapshot, self.db_path, self.backup_dir, label)
            if dest is not None:
                await asyncio.to_thread(rotate, self.backup_dir, self.config.keep)
                log.info("db backup -> %s", dest.name)
            return dest
        except Exception:
            log.exception("db backup failed")
            return None
