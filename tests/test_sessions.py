"""Per-visitor sessions (public embed hosting): each browser gets its own
player, so visitors can't pause, skip, or steal audio from each other."""

import asyncio
import json

from meshradio.bus import PLAYER_STATE

from .helpers import (
    Socket,
    client_for,
    embed_app,
    make_embed_player,
    make_ready_on,
    make_ready_track,
    page_app,
    peer,
    sid_of,
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
    async with client_for(app2, visited=False) as client:   # the returning visitor's first request
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
    async with client_for(app2, visited=False) as client:   # the returning visitor's first request
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
        await client.post("/api/volume/70")       # a press: the session is live now
        state = (await client.get("/api/state")).json()
        assert state["day"] == "2026-07-06" and state["status"] == "paused"
        assert app.state.sessions.count() == 1

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


async def test_new_day_song_rolls_idle_tabs_forward(db, bus):
    """Auto safety net: when a newer day's first song lands on the shared bus,
    an idle in-memory session advances on its own — no request or reload drives
    it — so an open tab updates live."""
    from meshradio.bus import TRACK_READY
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/volume/70")       # a press: the session is live now
        assert (await client.get("/api/state")).json()["day"] == "2026-07-06"
        sid = sid_of(client)
        await asyncio.sleep(0.05)   # let the day-watcher subscribe to the bus

        track = await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
        bus.publish(TRACK_READY, {"track": track})

        player = app.state.sessions._sessions[sid].player
        for _ in range(50):
            await asyncio.sleep(0.01)
            if player.day == "2026-07-07":
                break
        assert player.day == "2026-07-07"                 # advanced with no request
        assert player.current["video_id"] == "bbbbbbbbbbb"
        assert player.status == "paused"


async def test_playing_session_not_moved_by_new_day_song(db, bus):
    """The watcher never yanks a session that's actively playing."""
    from meshradio.bus import TRACK_READY
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")     # warm + playing
        sid = sid_of(client)
        await asyncio.sleep(0.05)
        track = await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
        bus.publish(TRACK_READY, {"track": track})
        await asyncio.sleep(0.1)
        player = app.state.sessions._sessions[sid].player
        assert player.day == "2026-07-06"                 # left playing where it was
        assert player.status == "playing"


def test_yt_export_url_dedupes_and_caps():
    from meshradio.web.context import YT_EXPORT_CAP, yt_export_url
    assert yt_export_url([]) == ""
    url = yt_export_url([{"video_id": "a"}, {"video_id": "b"}, {"video_id": "a"}])
    assert url.endswith("video_ids=a,b")                       # order kept, deduped
    many = yt_export_url([{"video_id": f"v{i:09d}"} for i in range(80)])
    assert many.count(",") == YT_EXPORT_CAP - 1                # capped


async def test_home_export_is_persistent_and_full_day(db, bus):
    """The now-playing export covers the whole day's songs and stays put once
    playback starts — not just what's still queued."""
    import re
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    await make_ready_on(db, "bbbbbbbbbbb", "2026-07-06")
    app = embed_app(db, bus)

    def export_ids(html):
        m = re.search(r"watch_videos\?video_ids=([A-Za-z0-9_,-]+)", html)
        return set(m.group(1).split(",")) if m else set()

    async with client_for(app) as client:
        before = export_ids((await client.get("/")).text)
        assert before == {"aaaaaaaaaaa", "bbbbbbbbbbb"}         # all songs, before playing
        await client.post("/api/play-day/2026-07-06")          # start playing
        after = export_ids((await client.get("/")).text)
        assert after == {"aaaaaaaaaaa", "bbbbbbbbbbb"}          # still all songs


async def test_session_cookie_issued_once(db, bus):
    app = embed_app(db, bus)
    async with client_for(app, visited=False) as client:
        first = await client.get("/api/state")
        assert "mr_sid" in first.cookies
        sid = first.cookies["mr_sid"]
        second = await client.get("/api/state")
        assert "mr_sid" not in second.cookies   # not re-issued
        assert client.cookies["mr_sid"] == sid


async def test_session_cap_evicts_stalest(db, bus):
    """Cookie-spraying bots can't grow the process without bound: at the cap,
    the stalest session is flushed to disk and evicted to make room."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    app.state.sessions.MAX_SESSIONS = 2
    async with client_for(app) as c1, client_for(app) as c2, client_for(app) as c3:
        await c1.post("/api/volume/70")                # a press opens each session
        sid1 = sid_of(c1)
        await c2.post("/api/volume/70")
        await c3.post("/api/volume/70")                # cap hit: c1 evicted
        assert app.state.sessions.count() == 2
        assert sid1 not in app.state.sessions._sessions
        assert await db.load_web_session(sid1) is not None   # snapshot kept
        # The evicted visitor comes back: session restores from disk.
        state = (await c1.get("/api/state")).json()
        assert state["current"]["video_id"] == "aaaaaaaaaaa"


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
    async with client_for(app2, visited=False) as client:   # the returning visitor's first request
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["status"] == "playing"
        assert state["current"]["video_id"] == "aaaaaaaaaaa"
        assert state["day"] == "2026-07-06"
        assert 30 <= state["position"] < 40


async def test_returning_session_picks_up_songs_posted_while_it_was_away(db, bus):
    """The morning case: a visitor loads the day with one song up and leaves,
    and the rest of the day's posts land while no player of theirs is running
    (the idle session was reaped, or the process restarted). Coming back they
    stay on the song they were on and find the others queued behind it —
    not just the one they saw, with the rest in the archive and nowhere else."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/volume/70")                 # a press: the session is live
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()

    for video_id in ("bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"):
        await make_ready_on(db, video_id, "2026-07-06")

    app2 = embed_app(db, bus)
    async with client_for(app2, visited=False) as client:   # the returning visitor's first request
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["current"]["video_id"] == "aaaaaaaaaaa"
        queued = [t["video_id"] for t in state["queue"]]
        assert queued == ["bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"]


async def test_returning_session_does_not_requeue_what_it_already_played(db, bus):
    """Catching up adds only what the day gained since the visitor last had
    it open; a song they played through, or took off the queue, stays gone."""
    for video_id in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc"):
        await make_ready_on(db, video_id, "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        await client.post("/api/skip")                      # past aaaaaaaaaaa
        sid = client.cookies["mr_sid"]
        await app.state.sessions.flush()

    await make_ready_on(db, "ddddddddddd", "2026-07-06")
    app2 = embed_app(db, bus)
    async with client_for(app2, visited=False) as client:
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["current"]["video_id"] == "bbbbbbbbbbb"
        assert [t["video_id"] for t in state["queue"]] == ["ccccccccccc", "ddddddddddd"]


async def test_snapshot_from_before_catch_up_still_restores(db, bus):
    """Sessions already on disk when this shipped carry no high-water mark.
    They are caught up from the furthest song they hold, so the deploy that
    adds the mark doesn't re-queue a visitor's whole day."""
    first = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    second = await make_ready_on(db, "bbbbbbbbbbb", "2026-07-06")
    await make_ready_on(db, "ccccccccccc", "2026-07-06")

    old = make_embed_player(db, bus)
    await old.cue_day("2026-07-06")
    await old.skip()                                        # a played, b current
    snap = old.snapshot()
    assert snap["current_track_id"] == second["id"] != first["id"]
    del snap["seen_track_id"]                               # the shape saved before the mark

    fresh = make_embed_player(db, bus)
    await fresh.restore(snap)
    assert fresh.current["video_id"] == "bbbbbbbbbbb"
    assert [t["video_id"] for t in fresh.queue] == ["ccccccccccc"]


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
    async with client_for(app2, visited=False) as client:   # the returning visitor's first request
        client.cookies.set("mr_sid", sid)
        state = (await client.get("/api/state")).json()
        assert state["status"] == "idle"               # graceful, not broken
        assert state["current"] is None


async def test_appliance_mode_still_shares_one_player(db, bus):
    """No factory (web/mpv appliance): the communal player handles everyone."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = page_app(db, bus)
    async with client_for(app) as alice, client_for(app) as bob:
        await alice.post("/api/play-day/2026-07-06")
        assert (await bob.get("/api/state")).json()["status"] == "playing"
        assert "mr_sid" not in (await bob.get("/api/state")).cookies


# -- who opens a session ------------------------------------------------------

async def test_page_views_open_no_session(db, bus):
    """A crawler walking the sitemap (no cookie jar) or a bot spraying fresh
    cookies used to mint a player, a task and a bus subscription per request.
    A GET now renders from a throwaway preview: still cued on the newest day,
    but nothing is kept."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        for _ in range(5):
            client.cookies.clear()
            page = await client.get("/")
            assert page.status_code == 200 and "mr_sid" in page.cookies
            state = (await client.get("/api/state")).json()   # now with that cookie
            assert state["status"] == "paused"
            assert state["current"]["video_id"] == "aaaaaaaaaaa"
        assert app.state.sessions.count() == 0
        assert await db.load_web_session(sid_of(client)) is None


async def test_a_press_opens_the_session(db, bus):
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.get("/api/state")
        assert app.state.sessions.count() == 0
        await client.post("/api/pause")                 # play
        assert app.state.sessions.count() == 1
        assert (await client.get("/api/state")).json()["status"] == "playing"


async def test_unknown_or_forged_cookie_opens_no_session_on_get(db, bus):
    """The cap's worst case: a bot presenting random valid-looking sids. One
    of the wrong shape is ignored and replaced; it never becomes a key."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app) as client:
        for i in range(5):
            client.cookies.set("mr_sid", f"{i:032x}")
            assert (await client.get("/api/state")).status_code == 200
        client.cookies.set("mr_sid", "x" * 4096)
        resp = await client.get("/api/state")
        assert "mr_sid" in resp.cookies and resp.cookies["mr_sid"] != "x" * 4096
        assert app.state.sessions.count() == 0
        assert "x" * 4096 not in app.state.sessions._sessions


async def test_websocket_opens_the_session(db, bus):
    """The page's socket is what turns a visitor into a session."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with client_for(app, visited=False) as client:
        sid = (await client.get("/")).cookies["mr_sid"]
    assert app.state.sessions.count() == 0
    sock = await Socket(app, cookie=sid).open()
    assert sock.accepted and sock.states()[0]["topic"] == "player.state"
    assert app.state.sessions.count() == 1
    await sock.close()


async def test_websocket_without_a_cookie_is_refused(db, bus):
    """No cookie means not our page; minting one here would open a session
    nothing could ever present again — one per bot connection."""
    app = embed_app(db, bus)
    # ...nor a well-formed value this server didn't sign.
    unsigned = f"{'a' * 32}.{'b' * 32}"
    for cookie in (None, "forged", "x" * 32, "a" * 32, unsigned):
        sock = await Socket(app, cookie=cookie).open()
        # Closed before accept, which the server reports as a 403 handshake.
        assert sock.refused_with == 1008 and len(sock.sent) == 1, cookie
        await sock.close()
    assert app.state.sessions.count() == 0


async def test_a_press_without_our_cookie_opens_nothing(db, bus):
    """A browser always carries the cookie it got with the page, so a POST
    with none — or with one this server didn't sign — is a script's. Each
    such press used to open a session and persist it: a player, a task and
    a row on disk per request, with the eviction churn once the cap hit."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    sessions = app.state.sessions
    for _ in range(5):
        async with client_for(app, visited=False) as bot:        # no cookie at all
            resp = await bot.post("/api/pause")
            assert resp.status_code == 200 and "mr_sid" in resp.cookies
    for forged in ("f" * 32, f"{'a' * 32}.{'b' * 32}", "x" * 4096):
        async with client_for(app, visited=False) as bot:
            bot.cookies.set("mr_sid", forged)
            resp = await bot.post("/api/pause")
            assert resp.status_code == 200 and "mr_sid" in resp.cookies   # reissued
    assert sessions.count() == 0
    await sessions.flush()
    row = await db._fetchone("SELECT COUNT(*) AS n FROM web_sessions")
    assert row["n"] == 0
    # The press after the page load — a cookie we signed — is the one that opens.
    async with client_for(app) as browser:
        await browser.post("/api/pause")
        assert sessions.count() == 1


async def test_the_signing_key_outlives_a_redeploy(db, bus):
    """The key lives in the archive with the snapshots, so a cookie issued
    before a redeploy still names its session after one."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    first = embed_app(db, bus)
    async with client_for(first) as client:
        await client.post("/api/play-day/2026-07-06")
        cookie = client.cookies["mr_sid"]
    await first.state.sessions.flush()
    second = embed_app(db, bus)                      # fresh process, same DB
    assert await second.state.sessions.secret() == await first.state.sessions.secret()
    async with client_for(second, visited=False) as client:
        client.cookies.set("mr_sid", cookie)
        resp = await client.get("/api/state")
        assert "mr_sid" not in resp.cookies          # recognised, not reissued
        assert resp.json()["current"]["video_id"] == "aaaaaaaaaaa"
        assert second.state.sessions.count() == 1


async def test_no_session_cookie_on_assets_health_feeds_or_the_relay(db, bus):
    """A stylesheet, the health check, a feed, the sitemap and a relay push
    carry no page a visitor could press anything on; a cookie on them was a
    token and a header per request, handed to crawlers, the host's health
    checker and the relay to present straight back."""
    app = embed_app(db, bus)
    async with client_for(app, visited=False) as client:
        for path in ("/static/style.css", "/healthz", "/robots.txt",
                     "/sitemap.xml", "/feed.xml"):
            resp = await client.get(path)
            assert resp.status_code == 200 and "mr_sid" not in resp.cookies, path
        resp = await client.post("/api/ingest")           # 404: no token configured
        assert resp.status_code == 404 and "mr_sid" not in resp.cookies
        assert "mr_sid" in (await client.get("/archive")).cookies   # a page still does


async def test_shutdown_flushes_and_stops_every_session(db, bus):
    """The lifespan's shutdown writes what the periodic flush hadn't yet and
    stops every player, so a deploy or restart loses nothing a visitor did."""
    await make_ready_track(db, "aaaaaaaaaaa", duration=60)
    app = embed_app(db, bus)
    async with app.router.lifespan_context(app):
        async with client_for(app) as client:
            await client.post("/api/play-day/2026-07-06")
            await client.post("/api/seek/42")
            sid = sid_of(client)
        assert app.state.sessions.count() == 1
        assert await db.load_web_session(sid) is None        # not flushed yet
    snap = json.loads(await db.load_web_session(sid))
    assert snap["status"] == "playing" and snap["position"] >= 42
    assert app.state.sessions.count() == 0
    assert app.state.sessions._maintenance is None


async def test_forwarding_headers_are_believed_only_from_a_trusted_proxy(db, bus):
    """A LAN client claiming https must not get an https canonical link or a
    Secure cookie it can't send back; a trusted proxy's word is taken."""
    headers = {"x-forwarded-proto": "https"}

    def trusting(proxies):
        return embed_app(db, bus, trusted_proxies=proxies)

    # The loopback peer is trusted by default: the test client, a local proxy.
    async with peer(trusting(["127.0.0.1"]), "127.0.0.1") as client:
        resp = await client.get("/", headers=headers)
        assert 'rel="canonical" href="https://test/"' in resp.text
        assert "secure" in resp.headers["set-cookie"].lower()
    # A client on the LAN saying the same is just a client saying things.
    async with peer(trusting(["127.0.0.1"]), "192.168.1.7") as client:
        resp = await client.get("/", headers=headers)
        assert 'rel="canonical" href="http://test/"' in resp.text
        assert "secure" not in resp.headers["set-cookie"].lower()
    # "*": every peer is the proxy — a host where nothing else can reach the app.
    async with peer(trusting(["*"]), "192.168.1.7") as client:
        resp = await client.get("/", headers=headers)
        assert 'rel="canonical" href="https://test/"' in resp.text
        assert "secure" in resp.headers["set-cookie"].lower()


async def test_pausing_an_archive_day_keeps_it(db, bus):
    """Play an older day from the Archive, then pause: the next request must
    leave the session on that day. Rolling a paused session forward is only
    for one parked on the day it landed on, not one the visitor picked."""
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        await client.post("/api/pause")
        state = (await client.get("/api/state")).json()
        assert state["status"] == "paused"
        assert state["day"] == "2026-07-06"                    # not yanked to the newest day
        assert state["current"]["video_id"] == "aaaaaaaaaaa"
        await client.post("/api/pause")                        # and resumes where it was
        state = (await client.get("/api/state")).json()
        assert state["status"] == "playing" and state["day"] == "2026-07-06"


async def test_paused_archive_day_not_moved_by_new_day_song(db, bus):
    """The day-watcher spares a day the visitor picked and paused, too."""
    from meshradio.bus import TRACK_READY
    await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    app = embed_app(db, bus)
    async with client_for(app) as client:
        await client.post("/api/play-day/2026-07-06")
        await client.post("/api/pause")
        sid = sid_of(client)
        await asyncio.sleep(0.05)
        track = await make_ready_on(db, "bbbbbbbbbbb", "2026-07-07")
        bus.publish(TRACK_READY, {"track": track})
        await asyncio.sleep(0.1)
        player = app.state.sessions._sessions[sid].player
        assert player.day == "2026-07-06"
        assert player.status == "paused"
