"""Input at the edge of the JSON API: values that parse but must not land.

Float path params accept ``inf`` and ``nan``. A seek to infinity left the
player's clock unserialisable (every state read 500'd), and a reported
duration of infinity went into the shared tracks row, breaking every session
that queued the song. Both are refused at the route now."""

import math
import time

from meshradio.db import MAX_SENDER, MAX_TITLE
from meshradio.web.server import _mmss

from .helpers import (
    client_for,
    embed_app,
    make_embed_player,
    make_ready_on,
    page_app,
    relay_embed_app,
)

NOT_A_LENGTH = ["inf", "-inf", "nan", "1e9", "-1"]


async def test_seek_rejects_non_finite_and_absurd_positions(db, bus):
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06", duration=60)
    async with client_for(embed_app(db, bus)) as client:
        assert (await client.post("/api/play-day/2026-07-06")).status_code == 303
        for bad in NOT_A_LENGTH:
            assert (await client.post(f"/api/seek/{bad}")).status_code == 422, bad
        # The player is untouched and still serialises.
        state = await client.get("/api/state")
        assert state.status_code == 200
        assert state.json()["position"] < 5
        resp = await client.post("/api/seek/30")
        assert resp.status_code == 200 and 30 <= resp.json()["position"] < 31


async def test_duration_report_fills_a_blank_only(db, bus):
    """The browser's duration report is unauthenticated and the row is shared:
    it may complete a missing length, never replace a known one."""
    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06", duration=None)
    async with client_for(embed_app(db, bus)) as client:
        await client.post("/api/play-day/2026-07-06")
        for bad in NOT_A_LENGTH + ["0"]:
            resp = await client.post(f"/api/duration/{track['id']}/{bad}")
            assert resp.status_code == 422, bad
        assert (await db.track_by_id(track["id"]))["duration"] is None

        assert (await client.post(f"/api/duration/{track['id']}/212.5")).status_code == 200
        assert (await db.track_by_id(track["id"]))["duration"] == 212.5
        # A second report (another tab, a replay, a prank) changes nothing.
        assert (await client.post(f"/api/duration/{track['id']}/999")).status_code == 200
        assert (await db.track_by_id(track["id"]))["duration"] == 212.5
        state = (await client.get("/api/state")).json()
        assert state["current"]["duration"] == 212.5


async def test_report_duration_guards_in_the_service_too(db, bus):
    """The route is the front door, but the OLED/other callers reach the
    service directly — it holds the same line on its own."""
    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06", duration=None)
    player = make_embed_player(db, bus)
    await player.play_track(await db.track_by_id(track["id"]))
    for bad in (math.inf, -math.inf, math.nan, 0, -3):
        await player.report_duration(track["id"], bad)
    assert player.current["duration"] is None
    await player.seek(math.inf)
    assert math.isfinite(player.position())
    await player.report_duration(track["id"], 100)
    await player.report_duration(track["id"], 200)
    assert (await db.track_by_id(track["id"]))["duration"] == 100
    assert player.current["duration"] == 100               # the clock follows the fill


async def test_relay_metadata_cannot_poison_the_shared_row(db, bus):
    """The relay's ``meta`` used to go straight into the row. One push carrying
    ``"duration": "inf"`` then broke the home page and the state API for every
    visitor — new sessions are cued onto the newest day, which is the day the
    push landed on — and nothing capped a title or a sender name."""
    app = relay_embed_app(db, bus)
    headers = {"Authorization": "Bearer s3cret"}
    now = time.time()
    async with client_for(app) as client:
        resp = await client.post("/api/ingest", headers=headers, json={"messages": [
            {"sender": "alice", "text": "Theme: test", "ts": now - 10},
            {"sender": "alice", "text": "https://youtu.be/aaaaaaaaaaa", "ts": now - 5,
             "meta": {"title": "T" * 5000, "artist": "A", "duration": "inf"}},
            {"sender": "S" * 500, "text": "https://youtu.be/bbbbbbbbbbb", "ts": now - 4,
             "meta": {"title": "ok", "duration": -3}},
        ]})
        assert resp.status_code == 200 and resp.json()["inserted"] == 2
        (bad,) = await db.tracks_for_video("aaaaaaaaaaa")
        assert bad["duration"] is None and len(bad["title"]) == MAX_TITLE
        (other,) = await db.tracks_for_video("bbbbbbbbbbb")
        assert len(other["sender"]) == MAX_SENDER and other["duration"] is None
        # A relayed track arrives titled, so the cacher marks it ready as-is
        # and the pages show it to everyone cued onto the day.
        for row in (bad, other):
            await db.set_cache_status(row["id"], "ready")
        for path in ("/", "/api/state", "/feed.xml"):
            assert (await client.get(path)).status_code == 200, path
        # Late metadata for a known track still fills in (the relay re-pushing
        # history), and a bad value in the same push can't undo a good one.
        await client.post("/api/ingest", headers=headers, json={"messages": [
            {"sender": "alice", "text": "https://youtu.be/aaaaaaaaaaa", "ts": now - 5,
             "meta": {"duration": 213}},
            {"sender": "alice", "text": "https://youtu.be/aaaaaaaaaaa", "ts": now - 5,
             "meta": {"duration": "nan"}},
        ]})
        assert (await db.tracks_for_video("aaaaaaaaaaa"))[0]["duration"] == 213


async def test_a_bad_length_already_in_a_row_cannot_break_a_page(db, bus):
    """Rows written before lengths were bounded (or edited by hand) may still
    hold one. The player's clock, the state JSON and the template filter all
    treat it as unknown rather than raising mid-render."""
    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06", duration=None)
    await db.db.execute("UPDATE tracks SET duration=1e999 WHERE id=?", (track["id"],))
    assert (await db.track_by_id(track["id"]))["duration"] == math.inf
    async with client_for(embed_app(db, bus)) as client:
        assert (await client.get("/")).status_code == 200
        assert (await client.post("/api/play-day/2026-07-06")).status_code == 303
        state = await client.get("/api/state")
        assert state.status_code == 200 and state.json()["current"]["duration"] is None
        assert (await client.post("/api/seek/30")).json()["position"] >= 30
    assert _mmss(math.inf) == "" and _mmss(math.nan) == "" and _mmss(-1) == ""
    assert _mmss(None) == "" and _mmss(3725) == "1:02:05"


async def test_output_routes_exist_only_on_the_appliance(db, bus):
    """Speaker, jack and Bluetooth are the appliance's to pick. The embed host
    has nothing to select, so the routes aren't there to answer for a no-op."""
    async with client_for(page_app(db, bus)) as client:
        assert (await client.get("/api/outputs")).status_code == 200
        assert (await client.post("/api/output/jack")).json()["output"] == "jack"
    async with client_for(embed_app(db, bus)) as client:
        assert (await client.get("/api/outputs")).status_code == 404
        assert (await client.post("/api/output/jack")).status_code == 404
