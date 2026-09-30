"""Input at the edge of the JSON API: values that parse but must not land.

Float path params accept ``inf`` and ``nan``. A seek to infinity left the
player's clock unserialisable (every state read 500'd), and a reported
duration of infinity went into the shared tracks row, breaking every session
that queued the song. Both are refused at the route now."""

import math

import pytest

from .test_sessions import client_for, embed_app, make_ready_on

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
    from meshradio.config import PlayerConfig
    from meshradio.media.player import EmbedBackend, PlayerService

    track = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06", duration=None)
    player = PlayerService(PlayerConfig(), db, bus, backend=EmbedBackend())
    await player.play_track(await db.track_by_id(track["id"]))
    for bad in (math.inf, -math.inf, math.nan, 0, -3):
        await player.report_duration(track["id"], bad)
    assert player.current["duration"] is None
    await player.seek(math.inf)
    assert math.isfinite(player.position())
    await player.report_duration(track["id"], 100)
    await player.report_duration(track["id"], 200)
    assert (await db.track_by_id(track["id"]))["duration"] == 100
