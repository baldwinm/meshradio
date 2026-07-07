"""Relay: the Pi pushes channel history to a hosted instance that Cloudflare
won't let poll CoreScope itself."""

import httpx

from meshradio.audio.routing import make_router
from meshradio.config import PlayerConfig, RelayConfig
from meshradio.db import Database
from meshradio.ingest.relay import CURSOR_KEY, RelayPusher
from meshradio.ingest.service import IngestService
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web.server import create_app


async def seed_history(db: Database):
    """One explicit theme, one auto theme, one channel track, one radio track."""
    theme = await db.create_theme(
        "2026-07-05", "games", set_by="alice", raw_message="Theme: games"
    )
    await db.create_theme("2026-07-04", "(untitled)")  # auto-created: not relayed
    await db.add_track(
        video_id="aaaaaaaaaaa",
        url="https://www.youtube.com/watch?v=aaaaaaaaaaa",
        channel="#music",
        sender="bob",
        mesh_ts=1_783_400_000.0,
        source="mesh",
        theme_id=theme["id"],
    )
    await db.add_track(
        video_id="bbbbbbbbbbb",
        url="https://www.youtube.com/watch?v=bbbbbbbbbbb",
        channel="radio",
        sender="radio",
        mesh_ts=1_783_400_100.0,
        source="radio",
        theme_id=None,
    )


async def test_collect_reconstructs_channel_messages(db, bus):
    await seed_history(db)
    pusher = RelayPusher(RelayConfig(), db)
    messages, newest = await pusher.collect("")
    # Explicit theme + channel track; auto theme and radio filler excluded.
    assert len(messages) == 2
    assert messages[0]["text"] == "Theme: games"      # sorted before its tracks
    assert messages[0]["sender"] == "alice"
    assert messages[1]["text"].endswith("aaaaaaaaaaa")
    assert messages[1]["sender"] == "bob"
    assert newest != ""


async def test_push_once_sends_auth_and_advances_cursor(db, bus):
    await seed_history(db)
    config = RelayConfig(push_url="https://radio.example.org/", token="s3cret")
    pusher = RelayPusher(config, db)
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"ok": True, "inserted": 2})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": f"Bearer {config.token}"},
    ) as client:
        pushed = await pusher.push_once(client)
        assert pushed == 2
        assert seen["url"] == "https://radio.example.org/api/ingest"
        assert seen["auth"] == "Bearer s3cret"
        # Cursor advanced: nothing left to push.
        assert await db.get_setting(CURSOR_KEY, "") != ""
        assert await pusher.push_once(client) == 0


def make_app(db, bus, token):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    ingest = IngestService(db, bus, channel="#music")
    router = make_router("dev", bus)
    return create_app(bus, db, player, router, ingest=ingest, ingest_token=token)


def api_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    )


MSG = {"sender": "carol", "text": "https://youtu.be/ccccccccccc", "ts": 1_783_400_200.0}


async def test_ingest_endpoint_disabled_without_token(db, bus):
    async with api_client(make_app(db, bus, token="")) as client:
        resp = await client.post("/api/ingest", json={"messages": [MSG]})
        assert resp.status_code == 404


async def test_ingest_endpoint_rejects_bad_token(db, bus):
    async with api_client(make_app(db, bus, token="s3cret")) as client:
        resp = await client.post(
            "/api/ingest",
            json={"messages": [MSG]},
            headers={"Authorization": "Bearer wrong"},
        )
        assert resp.status_code == 401


async def test_ingest_endpoint_inserts_and_dedupes(db, bus):
    async with api_client(make_app(db, bus, token="s3cret")) as client:
        headers = {"Authorization": "Bearer s3cret"}
        resp = await client.post(
            "/api/ingest",
            json={"messages": [MSG, {"bogus": True}]},
            headers=headers,
        )
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1   # malformed entry skipped
        # Replaying the same batch is a no-op thanks to ingest dedupe.
        resp = await client.post("/api/ingest", json={"messages": [MSG]}, headers=headers)
        assert resp.json()["inserted"] == 0
        days = await db.archive_days()
        assert len(days) == 1 and days[0]["tracks"] == 1
