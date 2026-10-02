"""Relay: the Pi pushes channel history to a hosted instance that Cloudflare
won't let poll CoreScope itself."""

import asyncio
import json

import httpx

from meshradio.bus import INGEST_STATUS, EventBus
from meshradio.config import RelayConfig
from meshradio.db import Database
from meshradio.ingest.relay import CURSOR_KEY, RelayPusher
from meshradio.ingest.service import IngestService

from .helpers import page_app


async def seed_history(db: Database):
    """One explicit theme, one auto theme, one channel track, one radio track."""
    theme = await db.create_theme(
        "2026-07-05", "games", set_by="alice", raw_message="Theme: games"
    )
    await db.create_theme("2026-07-04", "(untitled)")  # auto-created: not relayed
    track = await db.add_track(
        video_id="aaaaaaaaaaa",
        url="https://www.youtube.com/watch?v=aaaaaaaaaaa",
        channel="#music",
        sender="bob",
        mesh_ts=1_783_400_000.0,
        source="mesh",
        theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title="Song A", duration=213.0)
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
    # Metadata the home node already has rides along for the embed host.
    assert messages[1]["meta"] == {"title": "Song A", "duration": 213.0}
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


async def test_same_second_rows_not_skipped_nor_resent(db, bus):
    """Timestamps are second-resolution: a row committed in the same second
    as the cursor must still relay (id tiebreaker), and already-sent rows
    must not re-send every cycle."""
    theme = await db.create_theme("2026-07-05", "games", set_by="a", raw_message="Theme: games")
    await db.add_track(
        video_id="aaaaaaaaaaa", url="https://youtu.be/aaaaaaaaaaa", channel="#music",
        sender="bob", mesh_ts=1_783_400_000.0, source="mesh", theme_id=theme["id"],
    )
    await db.db.execute("UPDATE tracks SET ingested_at='2099-01-01T00:00:00Z'")
    await db.db.commit()
    pusher = RelayPusher(RelayConfig(), db)
    _, cursor = await pusher.collect("")

    # A second track lands in the very same second, after the cursor moved.
    await db.add_track(
        video_id="bbbbbbbbbbb", url="https://youtu.be/bbbbbbbbbbb", channel="#music",
        sender="carol", mesh_ts=1_783_400_060.0, source="mesh", theme_id=theme["id"],
    )
    await db.db.execute(
        "UPDATE tracks SET ingested_at='2099-01-01T00:00:00Z' WHERE video_id='bbbbbbbbbbb'"
    )
    await db.db.commit()

    messages, cursor2 = await pusher.collect(cursor)
    assert [m["sender"] for m in messages] == ["carol"]   # not skipped
    messages, _ = await pusher.collect(cursor2)
    assert messages == []                                 # not re-sent forever


async def test_adopted_theme_is_recollected(db, bus):
    """A placeholder skipped by an earlier push must come back once adopted —
    even when the adoption lands in the same second the placeholder was
    created, with the cursor sitting on exactly that row."""
    placeholder = await db.create_theme("2026-07-06", "Untitled — 2026-07-06")
    pusher = RelayPusher(RelayConfig(), db)
    messages, cursor = await pusher.collect("")
    assert messages == []                                  # placeholder skipped
    assert json.loads(cursor)["themes"] == [placeholder["created_at"], placeholder["id"]]

    await db.adopt_theme(placeholder["id"], "games", set_by="alice", raw_message="Theme: games")
    messages, cursor2 = await pusher.collect(cursor)
    assert [m["text"] for m in messages] == ["Theme: games"]
    messages, _ = await pusher.collect(cursor2)
    assert messages == []                                  # and not re-sent forever


async def test_adopted_theme_reaches_receiver(db, bus, tmp_path):
    """Links before the theme: the day opens with an "Untitled —" placeholder,
    which the relay skips, so the receiver builds its own. When the real
    "Theme: …" message adopts the placeholder, that adoption has to reach the
    receiver too — otherwise the hosted instance is stuck showing "Untitled"
    for the day until a wipe forces a full re-backfill."""
    day, ts = "2026-07-06", 1_783_400_000.0
    home = IngestService(db, bus, channel="#music")
    await home.handle_message(
        sender="bob", text="https://youtu.be/aaaaaaaaaaa", ts=ts, source="mesh"
    )

    receiver_db = Database(tmp_path / "receiver.db")
    await receiver_db.connect()
    receiver_bus = EventBus()
    pusher = RelayPusher(RelayConfig(push_url="https://receiver", token="s3cret"), db)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=make_app(receiver_db, receiver_bus, "s3cret")),
            headers={"Authorization": "Bearer s3cret"},
        ) as client:
            await pusher.push_once(client)
            remote = await receiver_db.latest_theme_for_date(day)
            assert remote["title"] == f"Untitled — {day}"   # its own placeholder

            await home.handle_message(
                sender="alice", text="Theme: games", ts=ts + 60, source="mesh"
            )
            assert (await db.latest_theme_for_date(day))["title"] == "games"

            await pusher.push_once(client)
            remote = await receiver_db.latest_theme_for_date(day)
            assert remote["title"] == "games"
            assert remote["set_by"] == "alice"
            # Adopted in place, so the day still has one playlist holding the
            # link that arrived before the theme.
            days = await receiver_db.archive_days()
            assert len(days) == 1 and days[0]["tracks"] == 1
    finally:
        await receiver_db.close()


async def test_legacy_plain_timestamp_cursor_still_works(db, bus):
    await seed_history(db)
    pusher = RelayPusher(RelayConfig(), db)
    messages, cursor = await pusher.collect("2020-01-01T00:00:00Z")
    assert len(messages) == 2          # everything after the legacy cursor
    assert cursor.startswith("{")      # upgraded to the structured form


async def test_wiped_receiver_triggers_rebackfill(db, bus):
    """Ephemeral hosting wipes the receiver's DB on deploys; the heartbeat
    push detects the count mismatch, resets the cursor, and re-pushes all."""
    await seed_history(db)
    pusher = RelayPusher(RelayConfig(push_url="https://r.example.org", token="t"), db)
    calls = []
    remote = {"tracks": 1}   # matches local channel_track_count after seed

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)["messages"]))
        return httpx.Response(200, json={"ok": True, "inserted": 0, **remote})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await pusher.push_once(client)
        assert calls == [2]              # initial backfill; counts match, no reset
        remote["tracks"] = 0             # receiver redeployed with a fresh DB
        pushed = await pusher.push_once(client)
        # heartbeat (0 msgs) sees the mismatch -> cursor reset -> full re-push
        assert calls == [2, 0, 2]
        assert pushed == 2


def make_app(db, bus, token):
    """The appliance with the relay receiver on."""
    return page_app(db, bus, ingest=IngestService(db, bus, channel="#music"), ingest_token=token)


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


async def test_healthz_shape(db, bus):
    async with api_client(make_app(db, bus, token="s3cret")) as client:
        body = (await client.get("/healthz")).json()
        assert body["ok"] is True
        assert body["tracks"] == 0
        assert body["ingest_age_s"] is None      # nothing ingested yet
        await client.post(
            "/api/ingest", json={"messages": [MSG]},
            headers={"Authorization": "Bearer s3cret"},
        )
        body = (await client.get("/healthz")).json()
        assert body["tracks"] == 1
        assert body["ingest_age_s"] is not None and body["ingest_age_s"] < 5


async def _settle(predicate, tries: int = 50):
    """Let the lifespan watcher task subscribe/drain between event-loop turns."""
    for _ in range(tries):
        await asyncio.sleep(0)
        if predicate():
            return True
    return False


async def test_healthz_fresh_from_backup_feed_alone(db, bus):
    """A poll from any analyzer feed marks ingest fresh. With only the primary
    counted, a CoreScope outage the backup feed was covering still read as
    "every ingest source stopped" and failed the host's health check."""
    app = make_app(db, bus, token="s3cret")
    async with app.router.lifespan_context(app), api_client(app) as client:
        assert (await client.get("/healthz")).json()["ingest_age_s"] is None
        await _settle(lambda: bool(bus._subs))
        bus.publish(INGEST_STATUS, {"corescope": "error", "comchan": "ok"})
        body = {}

        async def fresh():
            nonlocal body
            body = (await client.get("/healthz")).json()
            return body["ingest_age_s"] is not None

        for _ in range(50):
            await asyncio.sleep(0)
            if await fresh():
                break
        assert body["ingest_age_s"] is not None and body["ingest_age_s"] < 5


async def test_healthz_ignores_mesh_link_state(db, bus):
    """Mesh reports link state, not a completed ingest — a connected radio
    that has heard nothing must not pass for fresh ingestion."""
    app = make_app(db, bus, token="s3cret")
    async with app.router.lifespan_context(app), api_client(app) as client:
        await _settle(lambda: bool(bus._subs))
        bus.publish(INGEST_STATUS, {"mesh": "connected"})
        for _ in range(20):
            await asyncio.sleep(0)
        assert (await client.get("/healthz")).json()["ingest_age_s"] is None


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
        assert resp.json()["tracks"] == 1     # count for wipe detection
        # Replaying the same batch is a no-op thanks to ingest dedupe.
        resp = await client.post("/api/ingest", json={"messages": [MSG]}, headers=headers)
        assert resp.json()["inserted"] == 0
        days = await db.archive_days()
        assert len(days) == 1 and days[0]["tracks"] == 1



async def test_ingest_endpoint_rejects_the_wrong_shape_cleanly(db, bus):
    """Bad input is a 400 or a skipped row, never a 500 mid-batch."""
    async with api_client(make_app(db, bus, token="s3cret")) as client:
        headers = {"Authorization": "Bearer s3cret", "content-type": "application/json"}
        not_an_object = await client.post("/api/ingest", headers=headers, content=b"[1, 2]")
        assert not_an_object.status_code == 400
        assert (await client.post("/api/ingest", headers=headers, content=b"{{")).status_code == 400
        resp = await client.post(
            "/api/ingest", headers=headers,
            json={"messages": [1, None, {"sender": "a", "text": "t", "ts": "inf"},
                               {"sender": "a", "text": "t", "ts": "nan"}, MSG]},
        )
        assert resp.status_code == 200
        assert resp.json()["inserted"] == 1
        # A non-ASCII bearer value is just a wrong token (compare_digest
        # raises on non-ASCII str, which used to surface as a 500).
        resp = await client.post("/api/ingest", json={"messages": []},
                                 headers={b"authorization": "Bearer tok\xe9n".encode("latin-1")})
        assert resp.status_code == 401


async def test_ingest_endpoint_caps_a_chunked_body(db, bus):
    """No Content-Length (chunked transfer) must not bypass the size cap."""
    from meshradio.web import routes_ingest

    async def body():
        yield b'{"messages": ['
        for _ in range(4):
            yield b'"' + b"x" * 1024 + b'",'
        yield b'{}]}'

    app = make_app(db, bus, token="s3cret")
    async with api_client(app) as client:
        headers = {"Authorization": "Bearer s3cret", "content-type": "application/json"}
        saved, routes_ingest.MAX_INGEST_BYTES = routes_ingest.MAX_INGEST_BYTES, 2048
        try:
            resp = await client.post("/api/ingest", headers=headers, content=body())
        finally:
            routes_ingest.MAX_INGEST_BYTES = saved
        assert resp.status_code == 413


async def test_ingest_batch_is_one_transaction_per_chunk(db, bus):
    """A relay push of many messages lands in one commit per chunk, and a
    dedupe in the middle doesn't take the rest of the chunk with it."""
    commits = 0
    execute = db.db.execute

    async def counting(sql, *args, **kwargs):
        nonlocal commits
        if sql == "COMMIT":
            commits += 1
        return await execute(sql, *args, **kwargs)

    db.db.execute = counting
    msgs = [dict(MSG, text=f"https://youtu.be/{i:011d}", ts=MSG["ts"] + i) for i in range(30)]
    msgs.insert(15, dict(MSG, text="https://youtu.be/00000000003", ts=MSG["ts"] + 3))  # repost
    async with api_client(make_app(db, bus, token="s3cret")) as client:
        resp = await client.post("/api/ingest", json={"messages": msgs},
                                 headers={"Authorization": "Bearer s3cret"})
    assert resp.status_code == 200
    assert resp.json()["inserted"] == 30
    assert commits == 1               # the theme and every track rode one transaction
    assert len(await db.tracks_for_day("2026-07-06")) == 30


def test_push_url_must_be_https_off_the_box(db):
    """The token rides on every push; plain http may only stay on localhost."""
    import pytest

    from meshradio.ingest.relay import validate_push_url

    for ok in ("https://meshradio.example.org", "https://r.example.org:8443/base",
               "http://localhost:8080", "http://127.0.0.1:8080", "http://[::1]:8080"):
        assert validate_push_url(ok) == ok
        RelayPusher(RelayConfig(push_url=ok, token="t"), db)
    for bad in ("http://meshradio.example.org", "http://192.168.1.20:8080",
                "ftp://x.example", "meshradio.example.org", "https://"):
        with pytest.raises(ValueError, match="https"):
            validate_push_url(bad)
        with pytest.raises(ValueError):
            RelayPusher(RelayConfig(push_url=bad, token="t"), db)
    RelayPusher(RelayConfig(), db)                    # unset: the pusher never runs


async def test_a_receiver_reply_that_isnt_a_count_is_not_a_wipe(db, bus, caplog):
    """A track count that isn't a number is a receiver to look at, not a
    wiped archive to re-backfill; comparing it used to raise and end the push."""
    await seed_history(db)
    pusher = RelayPusher(RelayConfig(push_url="https://radio.example.org", token="s3cret"), db)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)["messages"]))
        return httpx.Response(200, json={"ok": True, "inserted": 0, "tracks": "lots"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await pusher.push_once(client)
        await pusher.push_once(client)
    assert calls == [2, 0]                         # history once, then a heartbeat
    assert "not a number" in caplog.text

    def not_even_an_object(request: httpx.Request) -> httpx.Response:
        calls.append(len(json.loads(request.content)["messages"]))
        return httpx.Response(200, json=[1, 2, 3])

    async with httpx.AsyncClient(transport=httpx.MockTransport(not_even_an_object)) as client:
        await pusher.push_once(client)
    assert calls == [2, 0, 0] and "not an object" in caplog.text
