import aiosqlite

from meshradio.db import MIGRATIONS, Database, dedupe_hash, utcnow

VID = "dQw4w9WgXcQ"


def _track_args(**overrides):
    args = dict(
        video_id=VID,
        url=f"https://www.youtube.com/watch?v={VID}",
        channel="#music",
        sender="alice",
        mesh_ts=1_751_800_000.0,
        source="mesh",
        theme_id=None,
    )
    args.update(overrides)
    return args


async def test_migrations_idempotent(db: Database):
    # Second migrate on the same file is a no-op.
    await db._migrate()
    assert await db.get_setting("nope") is None


async def test_dedupe_same_message(db: Database):
    first = await db.add_track(**_track_args())
    dupe = await db.add_track(**_track_args(source="corescope"))
    assert first is not None
    assert dupe is None  # corescope delivery of the same message no-ops


async def test_dedupe_bucket_60s(db: Database):
    ts = 1_751_800_020.0  # aligned to a 60s bucket start
    assert dedupe_hash("#music", "alice", VID, ts) == dedupe_hash("#music", "alice", VID, ts + 30)
    assert dedupe_hash("#music", "alice", VID, ts) != dedupe_hash("#music", "alice", VID, ts + 90)


async def test_repost_by_other_sender_is_new_track(db: Database):
    assert await db.add_track(**_track_args()) is not None
    assert await db.add_track(**_track_args(sender="bob")) is not None


async def test_malformed_video_id_rejected(db: Database):
    """A video id becomes a cache filename and a yt-dlp argument, so anything
    outside YouTube's 11-char id charset is refused at insert time."""
    for bad in ("../../etc/passwd", "-oExec", "aaa/bbb/ccc", "short", "a" * 40, ""):
        assert await db.add_track(**_track_args(video_id=bad)) is None
    # A well-formed id still inserts.
    assert await db.add_track(**_track_args()) is not None


async def test_theme_unique_per_day(db: Database):
    theme_a = await db.create_theme("2026-07-06", "rain songs", set_by="alice")
    theme_b = await db.create_theme("2026-07-06", "rain songs", set_by="bob")
    assert theme_a["id"] == theme_b["id"]


async def _build_v3_db(path):
    """A database migrated only through v3 (before the theme-merge migration)."""
    conn = await aiosqlite.connect(path)
    conn.row_factory = aiosqlite.Row
    for i, script in enumerate(MIGRATIONS[:3], start=1):
        await conn.executescript(script)
        await conn.execute(f"PRAGMA user_version={i}")
    await conn.commit()
    return conn


async def _add_theme_v3(conn, date, title, set_by=None):
    cur = await conn.execute(
        "INSERT INTO themes(date,title,set_by,created_at) VALUES(?,?,?,?) RETURNING id",
        (date, title, set_by, utcnow()),
    )
    row = await cur.fetchone()
    await conn.commit()
    return row["id"]


async def _add_track_v3(conn, theme_id, video_id):
    await conn.execute(
        "INSERT INTO tracks(video_id,url,ingested_at,source,dedupe_hash,theme_id) "
        "VALUES(?,?,?,?,?,?)",
        (video_id, f"https://y/{video_id}", utcnow(), "mesh", video_id, theme_id),
    )
    await conn.commit()


async def test_v4_merges_duplicate_day_themes(tmp_path):
    """The morning theme and a later rival for the same day collapse into one
    locked playlist, and the rival's tracks come along."""
    path = tmp_path / "legacy.db"
    conn = await _build_v3_db(path)
    morning = await _add_theme_v3(conn, "2026-07-06", "rain", set_by="alice")
    rival = await _add_theme_v3(conn, "2026-07-06", "hijack", set_by="mallory")
    await _add_track_v3(conn, morning, "aaaaaaaaaaa")
    await _add_track_v3(conn, rival, "bbbbbbbbbbb")
    await conn.close()

    db = Database(path)
    await db.connect()
    try:
        themes = await db.themes_for_day("2026-07-06")
        assert len(themes) == 1
        assert themes[0]["title"] == "rain"
        assert themes[0]["locked"] == 1
        assert len(await db.tracks_for_theme(themes[0]["id"])) == 2
    finally:
        await db.close()


async def test_v4_prefers_real_title_over_placeholder(tmp_path):
    """When a day split into a placeholder and a real theme, the merge keeps
    the real title (and locks it), regardless of insert order."""
    path = tmp_path / "legacy.db"
    conn = await _build_v3_db(path)
    placeholder = await _add_theme_v3(conn, "2026-07-06", "Untitled — 2026-07-06")
    real = await _add_theme_v3(conn, "2026-07-06", "rain", set_by="alice")
    await _add_track_v3(conn, placeholder, "aaaaaaaaaaa")
    await _add_track_v3(conn, real, "bbbbbbbbbbb")
    await conn.close()

    db = Database(path)
    await db.connect()
    try:
        themes = await db.themes_for_day("2026-07-06")
        assert len(themes) == 1
        assert themes[0]["title"] == "rain"
        assert themes[0]["locked"] == 1
        assert len(await db.tracks_for_theme(themes[0]["id"])) == 2
    finally:
        await db.close()


async def test_latest_theme_for_date(db: Database):
    await db.create_theme("2026-07-06", "first")
    second = await db.create_theme("2026-07-06", "second")
    latest = await db.latest_theme_for_date("2026-07-06")
    assert latest is not None and latest["id"] == second["id"]
    assert await db.latest_theme_for_date("2026-07-07") is None


async def test_cache_status_and_pending(db: Database):
    track = await db.add_track(**_track_args())
    assert [t["id"] for t in await db.pending_tracks()] == [track["id"]]
    await db.set_cache_status(track["id"], "ready", "/cache/x.opus")
    assert await db.pending_tracks() == []
    refreshed = await db.track_by_id(track["id"])
    assert refreshed["cache_status"] == "ready"
    assert refreshed["cache_path"] == "/cache/x.opus"


async def test_metadata_update_coalesces(db: Database):
    track = await db.add_track(**_track_args())
    await db.update_track_metadata(track["id"], title="Song", artist="Band", duration=200)
    await db.update_track_metadata(track["id"], duration=201)  # title/artist untouched
    refreshed = await db.track_by_id(track["id"])
    assert refreshed["title"] == "Song"
    assert refreshed["artist"] == "Band"
    assert refreshed["duration"] == 201


async def test_archive_queries(db: Database):
    theme = await db.create_theme("2026-07-06", "rain songs")
    await db.add_track(**_track_args(theme_id=theme["id"]))
    await db.add_track(**_track_args(theme_id=theme["id"], sender="bob", video_id="abcdefghijk"))
    days = await db.archive_days()
    assert len(days) == 1
    assert days[0]["date"] == "2026-07-06"
    assert days[0]["tracks"] == 2
    themes = await db.themes_for_day("2026-07-06")
    assert themes[0]["track_count"] == 2
    tracks = await db.tracks_for_theme(theme["id"])
    assert len(tracks) == 2
    assert len(await db.tracks_for_day("2026-07-06")) == 2


async def test_plays_and_lru_order(db: Database):
    theme = await db.create_theme("2026-07-06", "t")
    a = await db.add_track(**_track_args(theme_id=theme["id"]))
    b = await db.add_track(**_track_args(theme_id=theme["id"], video_id="abcdefghijk"))
    await db.set_cache_status(a["id"], "ready", "/cache/a.opus")
    await db.set_cache_status(b["id"], "ready", "/cache/b.opus")
    await db.record_play(a["id"], "speaker")
    lru = await db.cached_tracks_lru()
    # b never played -> evict first
    assert [t["id"] for t in lru] == [b["id"], a["id"]]


async def test_settings_roundtrip(db: Database):
    await db.set_setting("k", "v1")
    await db.set_setting("k", "v2")
    assert await db.get_setting("k") == "v2"
    assert await db.get_setting("missing", "dflt") == "dflt"
