"""Per-visitor sessions (public embed hosting): each browser gets its own
player, so visitors can't pause, skip, or steal audio from each other."""

import httpx

from meshradio.audio.routing import make_router
from meshradio.bus import EventBus, PLAYER_STATE
from meshradio.config import PlayerConfig
from meshradio.media.player import EmbedBackend, NullBackend, PlayerService
from meshradio.web.server import create_app

from .test_player import make_ready_track


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
        # Alice starts playing a day; Bob's radio must stay untouched.
        resp = await alice.post("/api/play-day/2026-07-06")
        assert resp.status_code == 303
        assert (await alice.get("/api/state")).json()["status"] == "playing"
        assert (await bob.get("/api/state")).json()["status"] == "idle"

        # Bob pausing his own (idle) player doesn't stop Alice's music.
        await bob.post("/api/pause")
        assert (await alice.get("/api/state")).json()["status"] == "playing"

        # Alice's session persists across her requests (same cookie).
        assert (await alice.get("/api/state")).json()["current"]["video_id"] == "aaaaaaaaaaa"


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


async def test_appliance_mode_still_shares_one_player(db, bus):
    """No factory (web/mpv appliance): the communal player handles everyone."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    app = create_app(bus, db, player, make_router("dev", bus))
    async with client_for(app) as alice, client_for(app) as bob:
        await alice.post("/api/play-day/2026-07-06")
        assert (await bob.get("/api/state")).json()["status"] == "playing"
        assert "mr_sid" not in (await bob.get("/api/state")).cookies
