"""Per-visitor sessions (public embed hosting): each browser gets its own
player, so visitors can't pause, skip, or steal audio from each other."""

import time

import httpx

from meshradio.audio.routing import make_router
from meshradio.bus import EventBus, PLAYER_STATE
from meshradio.config import PlayerConfig
from meshradio.media.player import EmbedBackend, NullBackend, PlayerService
from meshradio.web.server import create_app

from .test_player import make_ready_track


async def make_ready_on(db, video_id, date, duration=60):
    """A ready track filed under a specific archive day."""
    theme = await db.create_theme(date, f"theme {date}")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music", sender="alice", mesh_ts=time.time(),
        source="mesh", theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title=video_id, duration=duration)
    await db.set_cache_status(track["id"], "ready", f"/cache/{video_id}.opus")
    return await db.track_by_id(track["id"])


def embed_app(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=EmbedBackend())

    def factory(out_bus: EventBus) -> PlayerService:
        return PlayerService(
            PlayerConfig(), db, bus, backend=EmbedBackend(), events_out=out_bus
        )

    return create_app(
        bus, db, player, make_router("dev", bus), player_factory=factory
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


async def test_visitors_get_independent_players(db, bus):
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as alice, client_for(app) as bob:
        # Alice starts playing a day; Bob lands cued (paused), untouched.
        resp = await alice.post("/api/play-day/2026-07-06")
        assert resp.status_code == 303
        assert (await alice.get("/api/state")).json()["status"] == "playing"
        assert (await bob.get("/api/state")).json()["status"] == "paused"

        # Bob unpausing his own player doesn't affect Alice's session.
        await bob.post("/api/pause")
        alice_state = (await alice.get("/api/state")).json()
        assert alice_state["status"] == "playing"

        # Alice's session persists across her requests (same cookie).
        assert alice_state["current"]["video_id"] == "aaaaaaaaaaa"


async def test_new_visitor_lands_with_newest_day_cued(db, bus):
    """The landing page must never be an empty player: a fresh session gets
    the newest archive day loaded, parked at 0:00, one press from music."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        state = (await client.get("/api/state")).json()
        assert state["status"] == "paused"
        assert state["position"] == 0
        assert state["current"]["video_id"] == "aaaaaaaaaaa"
        assert state["day"] == "2026-07-06"
        # One press starts the day (toggle_pause on the cued player).
        await client.post("/api/pause")
        assert (await client.get("/api/state")).json()["status"] == "playing"


async def test_returning_session_moves_to_a_newer_day(db, bus):
    """A visitor parked (paused) on the day they first landed should jump to
    the newest day once a newer one exists, so the landing view stays current
    instead of showing yesterday's songs."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-06" and state["status"] == "paused"
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()

    # A newer day arrives; "redeploy" rebuilds the session from its snapshot.
    await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
    app2 = embed_app(db, bus)
    async with client_for(app2) as client:
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-07"                 # re-cued to newest
        assert state["current"]["video_id"] == "bbbbbbbbbbb"
        assert state["status"] == "paused"


async def test_returning_session_advances_even_if_snapshot_was_playing(db, bus):
    """A returning session is rebuilt from a snapshot (reap/redeploy), so its
    "playing" flag is stale — no audio is actually going on a fresh load. It
    must still land on the newest day, parked, rather than showing yesterday."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")       # snapshot says playing
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()

    await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
    app2 = embed_app(db, bus)
    async with client_for(app2) as client:
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-07"                 # rolled forward
        assert state["current"]["video_id"] == "bbbbbbbbbbb"
        assert state["status"] == "paused"


async def test_warm_idle_session_advances_when_a_new_day_arrives(db, bus):
    """The morning case: a live (in-memory) session parked on yesterday rolls
    forward to today on the next page load, without a restart."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-06" and state["status"] == "paused"

        await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")   # new day, same session
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-07"                    # rolled forward live
        assert state["current"]["video_id"] == "bbbbbbbbbbb"


async def test_warm_playing_session_is_not_interrupted(db, bus):
    """A genuinely-playing live session is never yanked to a newer day."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")          # warm + playing
        await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
        state = (await client.get("/api/state")).json()
        assert state["status"] == "playing"
        assert state["day"] == "2026-07-06"                    # left alone


async def test_session_cookie_issued_once(db, bus):
    app = embed_app(db, bus)
    async with client_for(app) as client:
        first = await client.get("/api/state")
        assert "mr_sid" in first.cookies
        sid = first.cookies["mr_sid"]
        second = await client.get("/api/state")
        assert "mr_sid" not in second.cookies   # not re-issued
        assert client.cookies["mr_sid"] == sid


async def test_session_state_stays_off_global_bus(db, bus):
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    global_sub = bus.subscribe(PLAYER_STATE)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        assert (await client.get("/api/state")).json()["status"] == "playing"
    # The session player's state announcements went to its private bus.
    assert global_sub.queue.qsize() == 0


async def test_session_survives_process_restart(db, bus):
    """Deploys restart the process: a returning cookie must restore its
    session (day, current track, position, queue) from the web_sessions
    table instead of landing on an idle player."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        await client.post("/api/seek/30")
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()   # what the maintenance loop does

    # "Redeploy": a brand-new app + manager over the same DB and cookie.
    app2 = embed_app(db, bus)
    async with client_for(app2) as client:
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["status"] == "playing"
        assert state["current"]["video_id"] == "aaaaaaaaaaa"
        assert state["day"] == "2026-07-06"
        assert 30 <= state["position"] < 40


async def test_restore_skips_vanished_tracks(db, bus):
    """A snapshot referencing tracks that lost readiness restores what it
    can instead of failing."""
    track = await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()
    await db.set_cache_status(track["id"], "failed")   # pruned/broken meanwhile

    app2 = embed_app(db, bus)
    async with client_for(app2) as client:
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["status"] == "idle"               # graceful, not broken
        assert state["current"] is None


async def test_appliance_mode_still_shares_one_player(db, bus):
    """No factory (web/mpv appliance): the communal player handles everyone."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    app = create_app(bus, db, player, make_router("dev", bus))
    async with client_for(app) as alice, client_for(app) as bob:
        await alice.post("/api/play-day/2026-07-06")
        assert (await bob.get("/api/state")).json()["status"] == "playing"
        assert "mr_sid" not in (await bob.get("/api/state")).cookies
