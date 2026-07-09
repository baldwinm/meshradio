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
    # A snapshot is one clean, self-contained file: no temp, no WAL sidecars.
    assert list(dest_dir.glob("*.part")) == []
    assert list(dest_dir.glob("*-wal")) == []
    assert list(dest_dir.glob("*-shm")) == []


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


async def test_resolve_snapshot(tmp_path):
    dest = tmp_path / "backups"
    dest.mkdir()
    older = dest / "meshradio-20260101T000000Z-auto.db"
    newer = dest / "meshradio-20260102T000000Z-auto.db"
    older.write_text("x")
    newer.write_text("x")
    assert backup.resolve_snapshot(dest, "latest") == newer
    assert backup.resolve_snapshot(dest, "") == newer
    assert backup.resolve_snapshot(dest, older.name) == older     # bare filename
    assert backup.resolve_snapshot(dest, str(older)) == older     # full path
    assert backup.resolve_snapshot(dest, "nope.db") is None
    assert backup.resolve_snapshot(tmp_path / "empty", "latest") is None


async def test_is_archive_db(tmp_path):
    src = tmp_path / "meshradio.db"
    await _seed(src)
    assert backup.is_archive_db(src) is True
    junk = tmp_path / "junk.db"
    junk.write_text("not a database")
    assert backup.is_archive_db(junk) is False
    assert backup.is_archive_db(tmp_path / "missing.db") is False


async def test_restore_replaces_db_and_keeps_a_safety_copy(tmp_path):
    db_path = tmp_path / "meshradio.db"
    await _seed(db_path)                       # 1 track
    backups = tmp_path / "backups"
    snap = backup.snapshot(db_path, backups, "auto")

    # Mutate the live DB (add a second track), then restore the snapshot.
    db = Database(db_path)
    await db.connect()
    theme = (await db.themes_for_day("2026-07-06"))[0]
    await db.add_track(video_id="abcdefghijk", url="https://y/abcdefghijk",
                       channel="#music", sender="bob", mesh_ts=1_751_900_000.0,
                       source="mesh", theme_id=theme["id"])
    await db.close()
    # leave a stale WAL sidecar behind to prove restore clears it
    (tmp_path / "meshradio.db-wal").write_text("stale")

    safety = backup.restore(db_path, snap, backups)
    assert safety is not None and safety.exists()      # reversible
    assert not (tmp_path / "meshradio.db-wal").exists()  # sidecar cleared

    db = Database(db_path)
    await db.connect()
    try:
        tracks = await db.tracks_for_day("2026-07-06")
    finally:
        await db.close()
    assert len(tracks) == 1                    # rolled back to the snapshot


async def test_restore_rejects_non_archive_file(tmp_path):
    db_path = tmp_path / "meshradio.db"
    await _seed(db_path)
    bad = tmp_path / "bad.db"
    bad.write_text("garbage")
    import pytest
    with pytest.raises(ValueError):
        backup.restore(db_path, bad, tmp_path / "backups")


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
