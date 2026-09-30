"""CoreScopePoller against the real API shape (captured from a live
CoreScope instance, 2026-07): GET /api/channels/{hash}/messages ->
{"messages": [...], "total": N}."""

import httpx
import pytest

from meshradio.config import ComchanConfig, CoreScopeConfig
from meshradio.db import Database
from meshradio.ingest.corescope import CURSOR_KEY, CoreScopePoller
from meshradio.ingest.service import IngestService

VID = "dQw4w9WgXcQ"
VID2 = "9bZkp7q19f0"


def corescope_msg(sender, text, sender_timestamp, first_seen, **extra):
    """A message as the CoreScope API actually returns it."""
    return {
        "first_seen": first_seen,
        "hops": 4,
        "observers": ["Some Repeater"],
        "packetHash": "936c9c6ac42a0b56",
        "packetId": 20513495,
        "repeats": 3,
        "sender": sender,
        "sender_timestamp": sender_timestamp,
        "snr": 11,
        "text": text,
        "timestamp": first_seen,
        **extra,
    }


NOON = 1_783_357_200  # 2026-07-06 12:00 CDT


@pytest.fixture
def poller_factory(db: Database, bus):
    def make(messages: list[dict]) -> tuple[CoreScopePoller, httpx.AsyncClient]:
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.raw_path.decode()
            return httpx.Response(200, json={"messages": messages, "total": len(messages)})

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://scope.example"
        )
        config = CoreScopeConfig(base_url="https://scope.example", channel="#music")
        service = IngestService(db, bus, channel="#music")
        poller = CoreScopePoller(config, service, db, bus)
        poller._captured = captured
        return poller, client

    return make


async def test_channel_hash_url_encoded(poller_factory):
    poller, client = poller_factory([])
    await poller.poll_once(client)
    assert poller._captured["path"] == "/api/channels/%23music/messages"


async def test_backfill_orders_theme_before_links(db, poller_factory):
    # Server returns newest-first; the theme post must still land first.
    messages = [
        corescope_msg("bob", f"https://youtu.be/{VID}", NOON + 300, "2026-07-06T17:05:10Z"),
        corescope_msg("alice", "Theme: songs about rain", NOON, "2026-07-06T17:00:05Z"),
    ]
    poller, client = poller_factory(messages)
    inserted = await poller.poll_once(client)
    assert inserted == 1
    themes = await db.themes_for_day("2026-07-06")
    assert themes[0]["title"] == "songs about rain"
    tracks = await db.tracks_for_theme(themes[0]["id"])
    assert tracks[0]["video_id"] == VID
    assert tracks[0]["source"] == "corescope"


async def test_cursor_skips_processed_messages(db, poller_factory):
    first_batch = [
        corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z"),
    ]
    poller, client = poller_factory(first_batch)
    assert await poller.poll_once(client) == 1
    assert await db.get_setting(CURSOR_KEY) == "2026-07-06T17:00:05Z"

    # Next poll returns full history again (no `since` param in the API)
    # plus one new message; only the new one should insert.
    second_batch = first_batch + [
        corescope_msg("bob", f"https://youtu.be/{VID2}", NOON + 600, "2026-07-06T17:10:11Z"),
    ]
    poller2, client2 = poller_factory(second_batch)
    assert await poller2.poll_once(client2) == 1
    assert await db.get_setting(CURSOR_KEY) == "2026-07-06T17:10:11Z"


async def test_cursor_tie_falls_through_to_dedupe(db, poller_factory):
    msg = corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")
    poller, client = poller_factory([msg])
    assert await poller.poll_once(client) == 1
    # Same first_seen as the cursor -> reprocessed, deduped, not double-counted.
    poller2, client2 = poller_factory([msg])
    assert await poller2.poll_once(client2) == 0


async def test_non_link_chatter_ignored(db, poller_factory):
    messages = [
        corescope_msg("carol", "yo music people", NOON, "2026-07-06T17:00:05Z"),
        corescope_msg("dave", "test", NOON + 60, "2026-07-06T17:01:05Z"),
    ]
    poller, client = poller_factory(messages)
    assert await poller.poll_once(client) == 0
    assert await db.archive_days() == []


async def test_malformed_message_skipped(db, poller_factory):
    messages = [
        {"sender": "x", "text": None, "sender_timestamp": NOON},   # no text
        {"sender": "y", "text": "hi"},                              # no timestamp
        corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z"),
    ]
    poller, client = poller_factory(messages)
    assert await poller.poll_once(client) == 1


def _secondary_poller(db, bus, messages):
    """A second CoreScope-compatible feed with its own name/source — the
    poller's generic multi-feed mechanism, independent of any one provider."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": messages, "total": len(messages)})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://backup.example.net"
    )
    config = CoreScopeConfig(channel="#music", base_url="https://backup.example.net")
    service = IngestService(db, bus, channel="#music")
    poller = CoreScopePoller(config, service, db, bus, name="backup", source="corescope")
    return poller, client


async def test_secondary_feed_keeps_its_own_cursor(db, bus):
    """A second feed keeps a separate cursor so it never clobbers the primary
    CoreScope cursor."""
    msg = corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")
    poller, client = _secondary_poller(db, bus, [msg])
    assert await poller.poll_once(client) == 1
    assert await db.get_setting("backup.cursor") == "2026-07-06T17:00:05Z"
    assert await db.get_setting(CURSOR_KEY) is None  # primary cursor untouched


async def test_secondary_feed_dedupes_against_primary(db, bus, poller_factory):
    """A message both feeds see inserts once — the second feed no-ops the
    primary's overlap instead of doubling every track."""
    msg = corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")
    primary, pclient = poller_factory([msg])
    assert await primary.poll_once(pclient) == 1
    backup, bclient = _secondary_poller(db, bus, [msg])
    assert await backup.poll_once(bclient) == 0
    assert len(await db.tracks_for_day("2026-07-06")) == 1


def _comchan_poller(db, bus, messages):
    """The backup feed exactly as app.py wires it: analyzer.comchan.net under
    the 'comchan' name and source."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"messages": messages, "total": len(messages)})

    config = ComchanConfig(channel="#music")
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=config.base_url
    )
    service = IngestService(db, bus, channel="#music")
    poller = CoreScopePoller(config, service, db, bus, name="comchan", source="comchan")
    return poller, client


async def test_comchan_feed_stamps_its_own_provenance(db, bus):
    """Backup-feed tracks are stamped 'comchan', not 'corescope', so it stays
    visible which analyzer supplied a day the primary was down for."""
    msg = corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")
    poller, client = _comchan_poller(db, bus, [msg])
    assert await poller.poll_once(client) == 1
    tracks = await db.tracks_for_day("2026-07-06")
    assert [t["source"] for t in tracks] == ["comchan"]
    assert await db.get_setting("comchan.cursor") == "2026-07-06T17:00:05Z"
    assert await db.get_setting(CURSOR_KEY) is None   # primary cursor untouched


async def test_comchan_feed_carries_a_day_the_primary_missed(db, bus, poller_factory):
    """The outage case this feed exists for: the primary returns nothing for a
    day, the backup has it, and the archive is whole either way."""
    theme = corescope_msg("alice", "Theme: songs about rain", NOON, "2026-07-06T17:00:05Z")
    song = corescope_msg("bob", f"https://youtu.be/{VID}", NOON + 300, "2026-07-06T17:05:10Z")
    primary, pclient = poller_factory([])
    assert await primary.poll_once(pclient) == 0
    backup, bclient = _comchan_poller(db, bus, [song, theme])
    assert await backup.poll_once(bclient) == 1
    themes = await db.themes_for_day("2026-07-06")
    assert themes[0]["title"] == "songs about rain"

    # When the primary comes back with the same history, it inserts nothing
    # new — the two feeds no-op each other rather than doubling the day.
    recovered, rclient = poller_factory([song, theme])
    assert await recovered.poll_once(rclient) == 0
    assert len(await db.tracks_for_day("2026-07-06")) == 1


async def test_comchan_defaults_to_a_configured_instance():
    """A backup nobody configured is no backup: the section may be absent from
    an appliance config written before it existed and must still poll."""
    assert ComchanConfig().enabled is True
    assert ComchanConfig().base_url == "https://analyzer.comchan.net"


def _client(handler, base="https://scope.example"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)


def _poller(db, bus):
    config = CoreScopeConfig(base_url="https://scope.example", channel="#music")
    return CoreScopePoller(config, IngestService(db, bus, channel="#music"), db, bus)


async def test_poll_refuses_an_oversized_history(db, bus, monkeypatch):
    """The analyzer returns the whole channel every poll; a response past the
    cap is refused while streaming rather than read into memory first."""
    from meshradio.ingest import corescope

    monkeypatch.setattr(corescope, "MAX_POLL_BYTES", 4096)
    huge = {"messages": [corescope_msg("a", "x" * 100, NOON, "2026-07-06T17:00:00Z")] * 100}
    served = {"bytes": 0}

    def handler(request):
        body = httpx.Response(200, json=huge).content
        served["bytes"] = len(body)

        async def chunked():
            yield body[:2048]
            yield body[2048:]

        # No Content-Length: chunked, so only counting what arrives can catch it.
        return httpx.Response(200, content=chunked(),
                              headers={"content-type": "application/json"})

    async with _client(handler) as client:
        with pytest.raises(corescope.PollTooLarge):
            await _poller(db, bus).poll_once(client)
    assert served["bytes"] > 4096
    assert await db.archive_days() == []                          # nothing ingested

    def declared(request):
        return httpx.Response(200, content=b"{}", headers={"content-length": "999999"})

    async with _client(declared) as client:
        with pytest.raises(corescope.PollTooLarge):
            await _poller(db, bus).poll_once(client)


async def test_poll_error_keeps_the_body_snippet(db, bus):
    """A Cloudflare challenge page and an origin error look the same by
    status; the first bytes of the body are what tell them apart."""
    def handler(request):
        return httpx.Response(403, text="<html>Just a moment... cf-challenge " + "x" * 500)

    async with _client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError) as blocked:
            await _poller(db, bus).poll_once(client)
    message = str(blocked.value)
    assert "HTTP 403" in message and "cf-challenge" in message
    assert len(message) < 400                                      # a snippet, not the page


async def test_poll_tolerates_a_malformed_history(db, bus):
    for body in (b"[]", b'{"messages": [1, null, {"text": "hi"}]}', b'{"messages": null}'):
        async with _client(lambda request, body=body: httpx.Response(200, content=body)) as client:
            assert await _poller(db, bus).poll_once(client) == 0
