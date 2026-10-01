"""CoreScopePoller against the real API shape.

The fake below serves ``/api/channels/{name}/messages`` the way CoreScope's
``handleChannelMessages`` does (upstream and the ComchanNet fork behind
analyzer.comchan.net are identical here, read from source 2026-10): the
response is ``{"messages": [...], "total": N}``; ``limit`` defaults to 100
and clamps to the instance's ``channelMessagesMax`` (500 by default);
``offset`` counts back from the newest message; each page comes out
oldest-first. The message objects are copied from a live capture."""

import re
from argparse import Namespace
from datetime import datetime, timezone

import httpx
import pytest

from meshradio import app as app_mod
from meshradio.config import ComchanConfig, Config, CoreScopeConfig
from meshradio.db import Database
from meshradio.ingest import corescope
from meshradio.ingest.corescope import CURSOR_KEY, PAGE_LIMIT, CoreScopePoller, probe
from meshradio.ingest.service import IngestService

VID = "dQw4w9WgXcQ"
VID2 = "9bZkp7q19f0"

NOON = 1_783_357_200  # 2026-07-06 12:00 CDT


def corescope_msg(sender, text, sender_timestamp, first_seen, **extra):
    """A message as the CoreScope API actually returns it."""
    msg = {
        "first_seen": first_seen,
        "hops": 4,
        "observers": ["Some Repeater"],
        "packetHash": "936c9c6ac42a0b56",
        "packetId": 20513495,
        "repeats": 3,
        "scope_name": None,
        "sender": sender,
        "sender_timestamp": sender_timestamp,
        "snr": 11,
        "text": text,
        "timestamp": first_seen,
    }
    msg.update(extra)
    return msg


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def link_msgs(n, *, start=NOON, sender="bob", step=30, first_id=0):
    """``n`` distinct song posts, one every ``step`` seconds from ``start``;
    video ids count up from ``first_id`` so two batches don't share songs."""
    return [
        corescope_msg(sender, f"https://youtu.be/vid{first_id + i:08d}", start + i * step,
                      iso(start + i * step), packetId=100_000 + first_id + i,
                      packetHash=f"{first_id + i:016x}")
        for i in range(n)
    ]


class FakeAnalyzer:
    """CoreScope's channel endpoints with their real paging semantics."""

    def __init__(self, messages, *, max_limit=500, honour_offset=True, channels=None):
        self.messages = list(messages)
        self.max_limit = max_limit
        self.honour_offset = honour_offset
        self.channels = channels
        self.requests: list[httpx.Request] = []

    @property
    def message_requests(self):
        return [r for r in self.requests if r.url.path.endswith("/messages")]

    def offsets(self):
        return [int(r.url.params.get("offset", 0)) for r in self.message_requests]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        raw_path = request.url.raw_path.decode().split("?")[0]
        if raw_path == "/api/channels":
            names = self.channels
            if names is None:
                names = ["#music"] if self.messages else []
            return httpx.Response(200, json={"channels": [
                {"hash": n, "name": n, "messageCount": len(self.messages),
                 "lastMessage": None, "lastSender": None, "lastActivity": "2026-07-06T17:00:05Z"}
                for n in names
            ]})
        match = re.fullmatch(r"/api/channels/([^/]+)/messages", raw_path)
        assert match, raw_path
        if match.group(1) != "%23music":
            return httpx.Response(200, json={"messages": [], "total": 0})
        params = request.url.params
        limit = _clamp(params.get("limit"), 100, self.max_limit)
        offset = int(params.get("offset", 0)) if self.honour_offset else 0
        # Newest at the tail, like the server's "rendered timestamp" sort.
        ordered = sorted(self.messages, key=lambda m: m["timestamp"])
        total = len(ordered)
        start = max(0, total - limit - offset)
        end = max(0, min(total, total - offset))
        return httpx.Response(200, json={"messages": ordered[start:end], "total": total})

    def client(self, base_url="https://scope.example") -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler), base_url=base_url)


def _clamp(raw, default, maximum):
    if raw is None:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    if n <= 0:
        return default
    return min(n, maximum)


@pytest.fixture
def poller_factory(db: Database, bus):
    def make(messages: list[dict], **analyzer_kwargs):
        analyzer = FakeAnalyzer(messages, **analyzer_kwargs)
        config = CoreScopeConfig(base_url="https://scope.example", channel="#music")
        service = IngestService(db, bus, channel="#music")
        poller = CoreScopePoller(config, service, db, bus)
        poller.analyzer = analyzer
        return poller, analyzer.client()

    return make


async def test_channel_hash_url_encoded(poller_factory):
    poller, client = poller_factory([])
    await poller.poll_once(client)
    request = poller.analyzer.message_requests[0]
    assert request.url.raw_path.decode().split("?")[0] == "/api/channels/%23music/messages"


async def test_asks_for_a_full_page_from_the_newest_end(poller_factory):
    """Sending no ``limit`` gets the newest 100 only — the poller asks for the
    instance's whole default page, starting at the end of the channel."""
    poller, client = poller_factory([])
    await poller.poll_once(client)
    params = poller.analyzer.message_requests[0].url.params
    assert PAGE_LIMIT == 500
    assert (params["limit"], params["offset"]) == ("500", "0")


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
        {"sender": "x", "text": None, "sender_timestamp": NOON, "timestamp": "2026-07-06T17:00:05Z"},
        {"sender": "y", "text": "hi", "timestamp": "x"},                # no usable time at all
        corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z"),
    ]
    poller, client = poller_factory(messages)
    assert await poller.poll_once(client) == 1


async def test_missing_sender_timestamp_falls_back_to_first_seen(db, poller_factory):
    """The decoder omits ``sender_timestamp`` when the radio sent none
    (``omitempty``); the analyzer's first sighting still dates the post."""
    msg = corescope_msg("alice", f"https://youtu.be/{VID}", None, "2026-07-06T17:00:05Z")
    del msg["sender_timestamp"]
    poller, client = poller_factory([msg])
    assert await poller.poll_once(client) == 1
    [track] = await db.tracks_for_day("2026-07-06")
    assert track["mesh_ts"] == 1_783_357_205.0            # 2026-07-06T17:00:05Z


# -- paging --------------------------------------------------------------------


async def test_backfill_walks_every_page(db, poller_factory, monkeypatch):
    """First boot: the channel is bigger than one page, and all of it lands."""
    monkeypatch.setattr(corescope, "PAGE_LIMIT", 40)
    messages = [corescope_msg("alice", "Theme: everything", NOON - 60, iso(NOON - 60))]
    messages += link_msgs(103)
    poller, client = poller_factory(messages, max_limit=40)
    assert await poller.poll_once(client) == 103
    assert poller.analyzer.offsets() == [0, 40, 80]
    assert len(await db.tracks_for_day("2026-07-06")) == 103
    themes = await db.themes_for_day("2026-07-06")
    assert themes[0]["title"] == "everything"               # landed before the links
    assert await db.get_setting(CURSOR_KEY) == iso(NOON + 102 * 30)


async def test_steady_poll_stops_at_the_cursor(db, poller_factory, monkeypatch):
    """After the backfill, a poll reads only as far back as its cursor: one
    page when nothing is new, one more when something is."""
    monkeypatch.setattr(corescope, "PAGE_LIMIT", 40)
    history = link_msgs(103)
    poller, client = poller_factory(history, max_limit=40)
    await poller.poll_once(client)

    quiet, qclient = poller_factory(history, max_limit=40)
    assert await quiet.poll_once(qclient) == 0
    assert quiet.analyzer.offsets() == [0]

    news = history + link_msgs(2, start=NOON + 200 * 30, sender="carol", first_id=500)
    busy, bclient = poller_factory(news, max_limit=40)
    assert await busy.poll_once(bclient) == 2
    assert busy.analyzer.offsets() == [0, 40]              # page 2 was all old -> stop
    assert len(await db.tracks_for_day("2026-07-06")) == 105


async def test_re_observed_old_message_does_not_end_the_walk(db, poller_factory, monkeypatch):
    """The server pages by latest observation, so a week-old post a repeater
    just re-heard sits on page 1 among genuinely new ones. Seeing something
    old on a page must not stop the walk while newer posts sit behind it."""
    monkeypatch.setattr(corescope, "PAGE_LIMIT", 3)
    old = link_msgs(4, start=NOON, sender="alice")
    poller, client = poller_factory(old, max_limit=3)
    assert await poller.poll_once(client) == 4

    new = link_msgs(4, start=NOON + 86_400, sender="bob", first_id=500)
    re_heard = dict(old[0], timestamp=iso(NOON + 86_400 + 1000))   # first_seen unchanged
    later, lclient = poller_factory(old[1:] + [re_heard] + new, max_limit=3)
    assert await later.poll_once(lclient) == 4
    assert later.analyzer.offsets() == [0, 3, 6]
    assert len(await db.tracks_for_day("2026-07-07")) == 4


async def test_server_that_clamps_the_page_size(db, poller_factory):
    """An operator can lower ``channelMessagesMax``; the walk advances by what
    each page held, not by what it asked for, so nothing is skipped."""
    poller, client = poller_factory(link_msgs(250), max_limit=100)
    assert await poller.poll_once(client) == 250
    assert poller.analyzer.offsets() == [0, 100, 200]


async def test_server_that_ignores_offset_is_read_once(db, poller_factory):
    """A build that hands the same page back for every offset: take the one
    copy and stop, rather than re-reading it until the count runs out."""
    poller, client = poller_factory(link_msgs(250), max_limit=100, honour_offset=False)
    assert await poller.poll_once(client) == 100
    assert poller.analyzer.offsets() == [0, 100]


async def test_poll_tolerates_a_response_without_a_total(db, bus):
    """Whatever the server says about the rest, one page without a count is
    the end of the walk."""
    def handler(request):
        return httpx.Response(200, json={"messages": [
            corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")]})

    async with _client(handler) as client:
        assert await _poller(db, bus).poll_once(client) == 1


# -- feeds ---------------------------------------------------------------------


def _secondary_poller(db, bus, messages):
    """A second CoreScope-compatible feed with its own name/source — the
    poller's generic multi-feed mechanism, independent of any one provider."""
    analyzer = FakeAnalyzer(messages)
    client = analyzer.client("https://backup.example.net")
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
    config = ComchanConfig(channel="#music")
    client = FakeAnalyzer(messages).client(config.base_url)
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


# -- limits and errors ---------------------------------------------------------


def _client(handler, base="https://scope.example"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base)


def _poller(db, bus):
    config = CoreScopeConfig(base_url="https://scope.example", channel="#music")
    return CoreScopePoller(config, IngestService(db, bus, channel="#music"), db, bus)


async def test_poll_refuses_an_oversized_history(db, bus, monkeypatch):
    """A response past the cap is refused while streaming rather than read
    into memory first."""
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


async def test_byte_cap_spans_the_whole_walk(db, poller_factory, monkeypatch):
    """The cap is per poll, not per page: a channel served in pages that each
    fit can't add up past it."""
    monkeypatch.setattr(corescope, "PAGE_LIMIT", 20)
    poller, client = poller_factory(link_msgs(60), max_limit=20)
    one_page = len(httpx.Response(200, json={"messages": link_msgs(20), "total": 60}).content)
    monkeypatch.setattr(corescope, "MAX_POLL_BYTES", one_page * 2)
    with pytest.raises(corescope.PollTooLarge):
        await poller.poll_once(client)
    assert poller.analyzer.offsets() == [0, 20, 40]
    assert await db.archive_days() == []                          # nothing ingested


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
    for body in (b"[]", b'{"messages": [1, null, {"text": "hi"}]}', b'{"messages": null}',
                 b'{"messages": {"a": 1}, "total": "many"}'):
        async with _client(lambda request, body=body: httpx.Response(200, content=body)) as client:
            assert await _poller(db, bus).poll_once(client) == 0


# -- probe (the live check) ----------------------------------------------------


async def test_probe_reports_the_api_shape():
    messages = [
        corescope_msg("alice", "Theme: songs about rain", NOON, "2026-07-06T17:00:05Z"),
        corescope_msg("bob", f"https://youtu.be/{VID}", NOON + 300, "2026-07-06T17:05:10Z"),
        {"sender": "ghost", "text": None, "timestamp": "2026-07-06T17:06:00Z"},
    ]
    analyzer = FakeAnalyzer(messages, channels=["#music", "#general"])
    async with analyzer.client() as client:
        report = await probe(client, "#music", name="comchan")
    assert report.ok and not report.error
    assert (report.total, report.served, report.parsed) == (3, 3, 2)
    assert report.channels == ["#music", "#general"] and report.channel_listed is True
    assert {"sender", "text", "timestamp", "first_seen", "sender_timestamp",
            "packetId", "packetHash", "repeats", "observers", "hops", "snr",
            "scope_name"} <= set(report.fields)
    assert [m["sender"] for m in report.newest] == ["bob", "alice"]   # newest first
    assert report.elapsed_s >= 0


async def test_probe_reports_a_block_without_raising():
    def handler(request):
        return httpx.Response(403, text="<html>Just a moment... cf-challenge " + "x" * 500)

    async with _client(handler) as client:
        report = await probe(client, "#music")
    assert not report.ok
    assert "HTTP 403" in report.error and "cf-challenge" in report.error
    assert report.served == 0 and report.newest == []


async def test_probe_flags_a_channel_the_analyzer_does_not_list():
    analyzer = FakeAnalyzer([], channels=["#general", "#bot-chatter"])
    async with analyzer.client() as client:
        report = await probe(client, "#music")
    assert report.ok                                   # the API answered; it is just empty
    assert report.channel_listed is False
    assert report.total == 0 and report.served == 0


async def test_probe_survives_a_broken_channel_listing():
    def handler(request):
        if request.url.path == "/api/channels":
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json={"messages": [
            corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")],
            "total": 1})

    async with _client(handler) as client:
        report = await probe(client, "#music")
    assert report.ok and report.parsed == 1
    assert report.channel_listed is None and "500" in report.channels_error


async def test_probe_feed_cli(tmp_path, capsys, monkeypatch):
    """``meshradio --probe-feed``: both feeds, the archive untouched, exit
    code says whether a probed feed failed."""
    config = Config(data_dir=tmp_path)
    config.corescope.base_url = "https://scope.example"
    outcomes = {
        "https://scope.example": FakeAnalyzer([
            corescope_msg("alice", f"https://youtu.be/{VID}", NOON, "2026-07-06T17:00:05Z")]),
        "https://analyzer.comchan.net": FakeAnalyzer([]),
    }
    monkeypatch.setattr(
        app_mod, "http_client",
        lambda **kw: outcomes[kw["base_url"]].client(kw["base_url"]),
    )

    assert await app_mod._run_probe_feed(config, Namespace(probe_feed="all")) == 0
    out = capsys.readouterr().out
    assert "corescope: https://scope.example  #music" in out
    assert "comchan: https://analyzer.comchan.net  #music" in out
    assert "fields: first_seen, hops," in out
    assert "alice: https://youtu.be/dQw4w9WgXcQ" in out
    assert out.count("ok (") == 2
    assert not config.db_path.exists()                         # never opened the archive

    outcomes["https://analyzer.comchan.net"] = _Blocked()
    assert await app_mod._run_probe_feed(config, Namespace(probe_feed="comchan")) == 1
    out = capsys.readouterr().out
    assert "FAILED" in out and "HTTP 403" in out and "corescope:" not in out

    config.comchan.enabled = False
    config.corescope.base_url = ""
    outcomes["https://analyzer.comchan.net"] = FakeAnalyzer([], channels=["#general"])
    assert await app_mod._run_probe_feed(config, Namespace(probe_feed="all")) == 0
    out = capsys.readouterr().out
    assert "corescope: no base_url configured" in out
    assert "enabled = false" in out
    assert "#music is NOT among them" in out
    assert await app_mod._run_probe_feed(config, Namespace(probe_feed="corescope")) == 1


class _Blocked:
    def client(self, base_url):
        return _client(lambda request: httpx.Response(403, text="Just a moment..."), base_url)
