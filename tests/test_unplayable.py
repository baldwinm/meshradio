"""Songs YouTube won't play: the embed player reports them, YouTube's oEmbed
confirms, and the song stops being offered until an admin tries it again."""

import time
from pathlib import Path

import meshradio.web as web
from meshradio.media import metadata

from .helpers import (
    admin_post,
    admin_settings,
    client_for,
    embed_app,
    make_ready_on,
    page_app,
    seed_shares,
    sign_in,
)

DAY = "2026-07-06"
LATER = "2026-07-08"
VID = "dQw4w9WgXcQ"
STATIC = Path(web.__file__).parent / "static"


def youtube_says(monkeypatch, answer):
    """Stub the oEmbed lookup; returns the list of video ids it was asked."""
    asked: list[str] = []

    async def fake(video_id, client=None):
        asked.append(video_id)
        return answer

    monkeypatch.setattr(metadata, "fetch_oembed", fake)
    return asked


async def reposted(db, title=None):
    """One video shared on two days, both playable."""
    rows = await seed_shares(db, VID, [DAY, LATER], title=title)
    for t in rows:
        await db.set_cache_status(t["id"], "ready")
    return [await db.track_by_id(t["id"]) for t in rows]


async def playing(client, date=DAY):
    await client.post(f"/api/play-day/{date}")
    return (await client.get("/api/state")).json()["current"]


async def test_a_removed_video_is_marked_on_every_share(db, bus, monkeypatch):
    asked = youtube_says(monkeypatch, None)
    first, repost = await reposted(db)
    async with client_for(embed_app(db, bus)) as client:
        cur = await playing(client)
        assert cur["id"] == first["id"]
        resp = await client.post(f"/api/unplayable/{cur['id']}/150")
        assert resp.json() == {"checking": True}
    assert asked == [VID]
    for t in (first, repost):
        assert (await db.track_by_id(t["id"]))["cache_status"] == "failed"
    async with client_for(embed_app(db, bus)) as client:
        await client.post(f"/api/play-day/{LATER}")
        assert (await client.get("/api/state")).json()["current"] is None
        page = (await client.get(f"/archive/{DAY}")).text
        assert "unavailable on YouTube" in page


async def test_a_video_youtube_still_describes_is_left_alone(db, bus, monkeypatch):
    """A region or age block (or a made-up report) gets an oEmbed answer."""
    youtube_says(monkeypatch, {"title": "Song", "artist": "A", "thumbnail": ""})
    track = await make_ready_on(db, VID, DAY)
    async with client_for(embed_app(db, bus)) as client:
        cur = await playing(client)
        await client.post(f"/api/unplayable/{cur['id']}/150")
    assert (await db.track_by_id(track["id"]))["cache_status"] == "ready"


async def test_only_the_current_song_and_real_video_errors_count(db, bus, monkeypatch):
    asked = youtube_says(monkeypatch, None)
    playing_now = await make_ready_on(db, VID, DAY)
    other = await make_ready_on(db, "9bZkp7q19f0", LATER)
    async with client_for(embed_app(db, bus)) as client:
        cur = await playing(client)
        assert cur["id"] == playing_now["id"]
        # A song not playing here, an HTML5 hiccup, and a made-up id.
        for path in (f"/api/unplayable/{other['id']}/150", f"/api/unplayable/{cur['id']}/5"):
            assert (await client.post(path)).json() == {"checking": False}
        assert (await client.post("/api/unplayable/999999/100")).json() == {
            "checking": False
        }
    assert asked == []
    assert (await db.track_by_id(other["id"]))["cache_status"] == "ready"


async def test_a_video_is_checked_at_most_once_an_hour(db, bus, monkeypatch):
    asked = youtube_says(monkeypatch, {"title": "Song", "artist": "A", "thumbnail": ""})
    await make_ready_on(db, VID, DAY)
    app = embed_app(db, bus)
    async with client_for(app) as one, client_for(app) as two:
        for client in (one, two, one):
            cur = await playing(client)
            await client.post(f"/api/unplayable/{cur['id']}/101")
        assert asked == [VID]
        app.state.unplayable_checks[VID] = time.monotonic() - 3601
        await one.post(f"/api/unplayable/{cur['id']}/101")
    assert asked == [VID, VID]


async def test_the_appliance_ignores_reports(db, bus, monkeypatch):
    """The Pi plays downloaded files; its failures are the cacher's to find."""
    asked = youtube_says(monkeypatch, None)
    track = await make_ready_on(db, VID, DAY)
    async with client_for(page_app(db, bus)) as client:
        await playing(client)
        assert (await client.post(f"/api/unplayable/{track['id']}/150")).json() == {
            "checking": False
        }
    assert asked == []


async def test_admin_lists_them_and_try_again_puts_every_share_back(
    db, bus, monkeypatch, tmp_path
):
    youtube_says(monkeypatch, None)
    first, repost = await reposted(db, title="Gone Song")
    app = embed_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app) as client:
        cur = await playing(client)
        await client.post(f"/api/unplayable/{cur['id']}/100")
    assert VID in app.state.unplayable_checks
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        page = (await client.get("/admin/removed")).text
        assert "Songs YouTube won't play" in page and "Gone Song" in page
        resp = await admin_post(client, f"/admin/tracks/{first['id']}/retry")
        assert resp.headers["location"] == "/admin/removed"
        assert "Every song plays." in (await client.get("/admin/removed")).text
    for t in (first, repost):
        assert (await db.track_by_id(t["id"]))["cache_status"] == "pending"
    assert VID not in app.state.unplayable_checks
    entries = await db.admin_log_entries("changes")
    assert [e["action"] for e in entries] == ["retry_track"]


async def test_the_device_admin_has_no_such_list(db, bus, tmp_path):
    """On the Pi a failed row is a failed download, shown on Device."""
    track = await make_ready_on(db, VID, DAY)
    await db.set_cache_status(track["id"], "failed")
    app = page_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        assert "Songs YouTube won't play" not in (await client.get("/admin/removed")).text


def test_the_embed_player_reports_before_it_skips():
    """The report only counts for the current song, so it has to reach the
    server before the skip moves the session on."""
    js = (STATIC / "js" / "embed.js").read_text()
    body = js[js.index("function onYtError"):js.index("function onYtState")]
    report = body.index('"/api/unplayable/"')
    skip = body.index('"/api/ended/"')
    assert ".then(ended, ended)" in body[report:]
    assert skip < report  # defined first, called only once the report returns
