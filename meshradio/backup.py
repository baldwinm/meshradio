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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import BackupConfig
from .runtime import Service

log = logging.getLogger(__name__)

_PREFIX = "meshradio-"


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
