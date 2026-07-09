import sqlite3
import time

from meshradio import backup
from meshradio.backup import BackupService
from meshradio.config import BackupConfig
from meshradio.db import Database


async def _seed(path):
    db = Database(path)
    await db.connect()
    theme = await db.create_theme("2026-07-06", "rain")
    await db.add_track(
        video_id="dQw4w9WgXcQ",
        url="https://y/dQw4w9WgXcQ",
        channel="#music",
        sender="alice",
        mesh_ts=1_751_800_000.0,
        source="mesh",
        theme_id=theme["id"],
    )
    await db.close()


async def test_snapshot_is_a_faithful_copy(tmp_path):
    src = tmp_path / "meshradio.db"
    await _seed(src)
    dest_dir = tmp_path / "backups"

    snap = backup.snapshot(src, dest_dir, label="auto")
    assert snap is not None and snap.exists()
    assert snap.parent == dest_dir
    # The copy is a valid, complete SQLite DB with the same rows.
    conn = sqlite3.connect(snap)
    try:
        (tracks,) = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()
        (themes,) = conn.execute("SELECT COUNT(*) FROM themes").fetchone()
    finally:
        conn.close()
    assert tracks == 1
    assert themes == 1
    # No leftover temp file.
    assert list(dest_dir.glob("*.part")) == []


async def test_snapshot_noop_when_no_db(tmp_path):
    assert backup.snapshot(tmp_path / "missing.db", tmp_path / "backups") is None
    assert not (tmp_path / "backups").exists() or list((tmp_path / "backups").iterdir()) == []


def test_rotate_keeps_newest(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    # Names carry a sortable timestamp; fabricate a chronological set.
    for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z",
                  "20260104T000000Z", "20260105T000000Z"):
        (dest / f"meshradio-{stamp}-auto.db").write_text("x")
    backup.rotate(dest, keep=2)
    left = sorted(p.name for p in dest.glob("meshradio-*.db"))
    assert left == ["meshradio-20260104T000000Z-auto.db", "meshradio-20260105T000000Z-auto.db"]


def test_rotate_keep_zero_is_noop(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    (dest / "meshradio-20260101T000000Z-auto.db").write_text("x")
    backup.rotate(dest, keep=0)
    assert len(list(dest.glob("meshradio-*.db"))) == 1


async def test_service_run_once_snapshots_and_prunes(tmp_path):
    src = tmp_path / "meshradio.db"
    await _seed(src)
    dest_dir = tmp_path / "backups"
    svc = BackupService(BackupConfig(keep=2), src, dest_dir)

    first = await svc.run_once()
    assert first is not None and first.exists()
    # Distinct filenames per second; nudge so the second snapshot sorts after.
    time.sleep(1.1)
    await svc.run_once()
    time.sleep(1.1)
    await svc.run_once()
    assert len(list(dest_dir.glob("meshradio-*.db"))) == 2  # pruned to keep
