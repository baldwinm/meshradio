"""The queries on the request path do the least work that answers them."""

import re
import time

import pytest

from .helpers import (
    client_for,
    counting,
    embed_app,
    make_embed_player,
    make_ready_on,
    page_app,
    seed_day,
)


async def test_newest_day_with_tracks(db):
    assert await db.newest_day_with_tracks() is None
    await db.create_theme("2026-08-09", "named but empty")      # no songs: not a landing day
    assert await db.newest_day_with_tracks() is None
    await seed_day(db, "2026-07-03", "aaaaaaaaaaa")
    await seed_day(db, "2026-08-01", "bbbbbbbbbbb")
    assert await db.newest_day_with_tracks() == "2026-08-01"


async def test_session_landing_does_not_aggregate_the_archive(db, bus):
    """Every request from an idle session parked on an older day re-checks
    for a newer one; that check must be the one-row lookup, not the
    whole-history aggregate."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/volume/70")                     # open a session
        # (that POST renders the now-playing partial, whose day arrows do use
        # the cached aggregate once; the session manager itself never should)
        aggregate = counting(db, "archive_days")
        lookup = counting(db, "newest_day_with_tracks")
        for _ in range(3):
            assert (await client.get("/api/state")).json()["day"] == "2026-07-06"
    assert aggregate["n"] == 0
    assert lookup["n"] >= 1


async def test_tracks_by_ids_batches_and_keys_by_id(db):
    theme = await db.create_theme("2026-07-06", "t")
    ids = []
    for i in range(7):
        track = await db.add_track(
            video_id=f"{i:011d}", url="u", channel="#music", sender="a",
            mesh_ts=1_783_400_000.0 + i, source="mesh", theme_id=theme["id"],
        )
        ids.append(track["id"])
    # Three round trips; the duplicates fold into one lookup each.
    rows = await db.tracks_by_ids(ids + [999_999] + ids[:2], chunk=3)
    assert set(rows) == set(ids)
    assert rows[ids[4]]["video_id"] == "00000000004"
    assert await db.tracks_by_ids([]) == {}


async def test_restore_fetches_the_queue_in_one_query(db, bus):
    tracks = [await make_ready_on(db, f"{i:011d}", "2026-07-06") for i in range(6)]
    player = make_embed_player(db, bus)
    per_id = counting(db, "track_by_id")
    batched = counting(db, "tracks_by_ids")
    await player.restore({
        "status": "paused", "current_track_id": tracks[0]["id"],
        "queue_track_ids": [t["id"] for t in tracks[1:]] + [123_456],   # one vanished
        "position": 12, "saved_at": time.time(),
    })
    assert per_id["n"] == 0 and batched["n"] == 1
    assert player.current["id"] == tracks[0]["id"]
    # Order kept, the vanished id skipped.
    assert [t["id"] for t in player.queue] == [t["id"] for t in tracks[1:]]


async def test_stats_and_theme_pages_are_cached_briefly(db, bus):
    """Both pages are in the sitemap and recomputed whole-archive aggregates
    per hit; within the TTL a second hit is served from memory."""
    await seed_day(db, "2026-08-01", "aaaaaaaaaaa")
    app = page_app(db, bus)
    stats = counting(db, "overall_stats")
    themes = counting(db, "all_themes")
    async with client_for(app) as client:
        for _ in range(3):
            assert (await client.get("/stats")).status_code == 200
            assert (await client.get("/archive/themes")).status_code == 200
    assert stats["n"] == 1 and themes["n"] == 1
    # Aged out, it recomputes.
    ctx = app.state.ctx
    for key in ("stats", "themes"):
        at, value = ctx._cache[key]
        ctx._cache[key] = (at - ctx.CACHE_TTL_S - 1, value)
    async with client_for(app) as client:
        await client.get("/stats")
        await client.get("/archive/themes")
    assert stats["n"] == 2 and themes["n"] == 2


@pytest.mark.parametrize("sql, params, index", [
    pytest.param(
        "SELECT * FROM tracks WHERE cache_status='ready' AND cache_path IS NOT NULL "
        "ORDER BY last_played_at, ingested_at LIMIT 50", (), "idx_tracks_lru",
        id="prune candidates walk the LRU index in order"),
    pytest.param(
        "SELECT COUNT(*) FROM tracks WHERE sender = ? COLLATE NOCASE AND source != 'radio'",
        ("Ana",), "idx_tracks_sender", id="member lookup finds the NOCASE index"),
    pytest.param(
        "SELECT rowid FROM tracks_fts WHERE tracks_fts MATCH ?", ('"rain"',),
        "VIRTUAL TABLE INDEX", id="search of three chars or more is the FTS index"),
])
async def test_hot_queries_come_from_indexes(db, sql, params, index):
    """No scan of tracks and no sort step on the request path: the pruner,
    the member pages (four lookups per visit) and search each have an
    index (migrations v12 and v13) and the planner must pick it."""
    details = [r["detail"] for r in await db._fetchall("EXPLAIN QUERY PLAN " + sql, params)]
    assert any(index in d for d in details), details
    assert not any("TEMP B-TREE" in d or re.match(r"SCAN tracks\b", d) for d in details), details
