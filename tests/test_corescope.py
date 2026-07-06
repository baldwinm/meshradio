"""CoreScopePoller against the real API shape (captured from a live
CoreScope instance, 2026-07): GET /api/channels/{hash}/messages ->
{"messages": [...], "total": N}."""

import httpx
import pytest

from meshradio.config import CoreScopeConfig
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
