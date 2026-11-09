"""YouTube's captcha block: when Google refuses the watch page to this
server's address, every reader of it stands down together, logs one line,
and picks up again once the block ends."""

import logging

import httpx

from meshradio.media import metadata
from meshradio.media.durations import DurationService

from .helpers import (
    art_track_description,
    captcha_redirect,
    embed_cacher,
    make_pending_track,
    make_ready_on,
    watch_page,
)


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_a_captcha_redirect_pauses_every_watch_page_lookup(caplog):
    seen = []

    def handler(request):
        seen.append(request.url.params["v"])
        return captcha_redirect(request.url.params["v"])

    caplog.set_level(logging.WARNING, logger="meshradio.media.metadata")
    async with _client(handler) as client:
        assert await metadata.fetch_duration("aaaaaaaaaaa", client) is None
        assert metadata.watch_blocked()
        assert 0 < metadata.watch_block_remaining() <= metadata.BLOCK_S
        # Neither lengths nor credits ask again while it lasts.
        assert await metadata.fetch_duration("bbbbbbbbbbb", client) is None
        assert await metadata.fetch_watch_credits("ccccccccccc", client) is None
    assert seen == ["aaaaaaaaaaa"]
    # One plain line, no traceback.
    records = [r for r in caplog.records if r.name == "meshradio.media.metadata"]
    assert len(records) == 1
    assert records[0].exc_info is None
    assert "captcha" in records[0].getMessage()


async def test_too_many_requests_is_a_block_too():
    async with _client(lambda request: httpx.Response(429)) as client:
        assert await metadata.fetch_duration("aaaaaaaaaaa", client) is None
    assert metadata.watch_blocked()


async def test_other_failures_are_not_a_block(caplog):
    caplog.set_level(logging.WARNING, logger="meshradio.media.metadata")
    async with _client(lambda request: httpx.Response(500)) as client:
        assert await metadata.fetch_duration("aaaaaaaaaaa", client) is None
    consent = httpx.Response(302, headers={"location": "https://consent.youtube.com/m"})
    async with _client(lambda request: consent) as client:
        assert await metadata.fetch_duration("aaaaaaaaaaa", client) is None
    assert not metadata.watch_blocked()
    assert all(r.exc_info is None for r in caplog.records)


async def test_lookups_resume_when_the_block_ends(monkeypatch):
    async with _client(lambda request: captcha_redirect()) as client:
        await metadata.fetch_duration("aaaaaaaaaaa", client)
    assert metadata.watch_blocked()
    monkeypatch.setattr(metadata, "_blocked_until", 0.0)
    page = watch_page(art_track_description())
    async with _client(lambda request: httpx.Response(200, text=page)) as client:
        assert await metadata.fetch_duration("aaaaaaaaaaa", client) == 200.0


async def test_a_refused_length_is_not_counted_against_the_video(db, bus, monkeypatch):
    async def refused(video_id, client=None):
        monkeypatch.setattr(metadata, "_blocked_until", float("inf"))
        return None

    monkeypatch.setattr(metadata, "fetch_duration", refused)
    await make_ready_on(db, "aaaaaaaaaaa", "2026-08-01", duration=None)
    service = DurationService(db, bus)
    for _ in range(DurationService.MAX_ATTEMPTS + 2):
        assert await service.resolve("aaaaaaaaaaa") is True
        assert service._next() == "aaaaaaaaaaa"     # first in line once it ends
    assert service._attempts == {}
    assert service._streak == 0


async def test_an_art_track_refused_its_credits_gets_them_after_the_block(
    db, bus, monkeypatch, tmp_path
):
    c, calls = embed_cacher(db, bus, tmp_path, monkeypatch, "Release - Topic", None)
    monkeypatch.setattr(metadata, "_blocked_until", float("inf"))
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await c.process_track(track)
    row = await db.track_by_id(track["id"])
    assert row["artist"] is None and row["cache_status"] == "ready"

    # Still blocked: nothing is asked.
    calls.clear()
    await c._retry_credits()
    assert calls == []

    async def credits(video_id, client=None):
        calls.append(video_id)
        return "Real Artist"

    monkeypatch.setattr(metadata, "_blocked_until", 0.0)
    monkeypatch.setattr(metadata, "fetch_watch_credits", credits)
    await c._retry_credits()
    assert (await db.track_by_id(track["id"]))["artist"] == "Real Artist"
    await c._retry_credits()                       # asked once, then forgotten
    assert calls == ["aaaaaaaaaaa"]


async def test_a_song_without_credits_outside_a_block_is_not_retried(
    db, bus, monkeypatch, tmp_path
):
    c, calls = embed_cacher(db, bus, tmp_path, monkeypatch, "Release - Topic", None)
    track = await make_pending_track(db, "aaaaaaaaaaa")
    await c.process_track(track)
    calls.clear()
    await c._retry_credits()
    assert calls == []
