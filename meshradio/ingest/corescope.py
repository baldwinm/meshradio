"""CoreScope-compatible analyzer poller — fallback ingestion and first-boot
backfill.

Written against CoreScope's real API. The primary runs
github.com/Kpa-clawbot/CoreScope (verified 2026-07 against a live instance);
the backup, analyzer.comchan.net, runs the ComchanNet/CoreScope fork, whose
channel endpoint is byte-for-byte upstream's (read from both sources,
2026-10 — the host is not reachable from every network, see ``probe``):

    GET /api/channels/{hash}/messages?limit=N&offset=M
        -> {"messages": [...], "total": T}

``hash`` is the channel *name*, URL-encoded (``#music`` -> ``%23music``).
Each message carries ``sender``, ``text``, ``sender_timestamp`` (unix
seconds from the sending radio — the same value the local node sees, which
is what makes cross-source dedupe line up; omitted when the radio sent
none), ``first_seen`` and ``timestamp`` (ISO, the analyzer's own clock),
plus packet metadata this poller ignores (``packetId``, ``packetHash``,
``repeats``, ``observers``, ``hops``, ``snr``, ``scope_name``).

There is no ``since`` parameter, but there is paging: ``limit`` defaults to
100 and is clamped to the instance's ``channelMessagesMax`` (500 unless the
operator changed it), and ``offset`` counts back from the newest message.
A request that sends neither gets the newest 100 — enough between polls,
but no first-boot backfill and no recovery after a long outage. So the
poller walks the pages from the end, newest first, until a page holds
nothing newer than what it already processed (its cursor on ``first_seen``,
kept in settings) or it runs off the start of the channel. Cursor ties, the
one-message overlap a post arriving between two pages shifts in, and
late-arriving RF duplicates all fall through to the dedupe hash, which
makes reprocessing a no-op.

The ``name``/``source`` params keep the poller reusable for any additional
CoreScope-compatible feed (its own cursor key, status field, and track
provenance); because dedupe keys on channel+sender+video+minute rather than
source, two such feeds no-op each other's overlap. app.py uses that to run
the ``comchan`` backup feed alongside the primary — concurrently, not failed
over to, so neither instance's outage stalls ingestion and neither needs
health tracking to decide who is in charge.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx

from ..bus import INGEST_STATUS, EventBus
from ..config import CoreScopeConfig
from ..db import Database
from ..net import http_client
from ..runtime import Service
from .service import IngestService

log = logging.getLogger(__name__)

CURSOR_KEY = "corescope.cursor"

# Messages committed per transaction during a poll. Big enough that a backfill
# is a handful of commits; small enough that other writers (a play starting,
# a session flush) wait milliseconds, not the whole history.
INGEST_BATCH = 500

# Messages asked for per page: CoreScope's default ``channelMessagesMax``.
# An instance configured lower clamps silently, so the walk advances by what
# each page actually held, never by this.
PAGE_LIMIT = 500

# Ceiling on what one poll reads across all its pages. A year of a busy
# channel is a few megabytes; this is the point past which the response is
# not a channel any more, and reading on would only be growing our memory
# at the analyzer's say-so.
MAX_POLL_BYTES = 64 * 1024 * 1024


class PollTooLarge(Exception):
    """The analyzer's response outgrew MAX_POLL_BYTES; nothing was ingested."""


class CoreScopePoller(Service):
    def __init__(
        self,
        config: CoreScopeConfig,
        service: IngestService,
        db: Database,
        bus: EventBus,
        *,
        name: str = "corescope",
        source: str = "corescope",
    ):
        # ``name`` scopes the poll cursor and the INGEST_STATUS field so a
        # second, identically-shaped feed doesn't clobber the primary's cursor.
        # ``source`` is the provenance stamped on tracks.
        self.config = config
        self.service = service
        self.db = db
        self.bus = bus
        self.name = name
        self.source = source
        self.cursor_key = f"{name}.cursor"

    async def _run(self) -> None:
        if not self.config.base_url:
            log.warning("%s base_url not configured; poller idle", self.name)
            self.bus.publish(INGEST_STATUS, {self.name: "unconfigured"})
            return
        async with http_client(base_url=self.config.base_url) as client:
            while True:
                try:
                    await self.poll_once(client)
                    self.bus.publish(INGEST_STATUS, {self.name: "ok"})
                except httpx.HTTPStatusError as exc:
                    # The message carries a body snippet (see _fetch_page):
                    # it tells a Cloudflare block apart from an origin error
                    # without dumping a whole challenge page.
                    log.error("%s poll: %s", self.name, exc)
                    self.bus.publish(INGEST_STATUS, {self.name: "error"})
                except Exception:
                    log.exception("%s poll failed", self.name)
                    self.bus.publish(INGEST_STATUS, {self.name: "error"})
                await asyncio.sleep(self.config.poll_interval_s)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        cursor = await self.db.get_setting(self.cursor_key, "") or ""
        messages = await fetch_history(client, self.config.channel, since=cursor)
        # Skip what previous polls handled; include cursor ties (dedupe
        # no-ops them) so nothing sharing a first_seen second is lost.
        fresh = [m for m in messages if not cursor or m["first_seen"] >= cursor]
        # Themes must land before the links posted after them.
        fresh.sort(key=lambda m: m["ts"])
        inserted = 0
        # One commit per batch rather than per row: a first-boot backfill is
        # thousands of messages, each otherwise its own write transaction.
        for start in range(0, len(fresh), INGEST_BATCH):
            async with self.db.transaction():
                for msg in fresh[start:start + INGEST_BATCH]:
                    inserted += await self.service.handle_message(
                        sender=msg["sender"], text=msg["text"], ts=msg["ts"], source=self.source
                    )
        newest = max((m["first_seen"] for m in fresh), default="")
        if newest and newest != cursor:
            await self.db.set_setting(self.cursor_key, newest)
        if inserted:
            log.info("%s poll: %d new tracks", self.name, inserted)
        return inserted


# -- API adapter ---------------------------------------------------------------


async def fetch_history(
    client: httpx.AsyncClient, channel: str, *, since: str = ""
) -> list[dict[str, Any]]:
    """Every message of ``channel`` newer than ``since`` (a ``first_seen``
    value; empty means the whole channel), walked page by page from the
    newest end. Pages come back newest-page-first and ascending within, so
    callers sort before relying on order.

    The walk ends at a page with nothing newer than ``since``, at the start of
    the channel (``offset`` past ``total``), or at a server that is not paging
    at all (an older build hands the same page back for every ``offset``;
    one copy is enough). The byte ceiling spans the whole walk."""
    path = _messages_path(channel)
    budget = _ByteBudget(MAX_POLL_BYTES)
    offset = 0
    out: list[dict[str, Any]] = []
    previous_key = None
    while True:
        page = await _fetch_page(client, path, PAGE_LIMIT, offset, budget)
        if not page.raw:
            break
        key = _page_key(page.raw)
        if key == previous_key:
            break
        previous_key = key
        out.extend(page.messages)
        offset += len(page.raw)
        if page.total is None or offset >= page.total:
            break
        dated = [m["first_seen"] for m in page.messages if m["first_seen"]]
        if since and dated and max(dated) <= since:
            break
    return out


@dataclass
class Page:
    messages: list[dict[str, Any]]   # normalized, in the server's order
    raw: list[Any]                   # as served, malformed entries included
    total: int | None                # the channel's deduplicated message count


async def _fetch_page(
    client: httpx.AsyncClient, path: str, limit: int, offset: int, budget: _ByteBudget
) -> Page:
    """One page of channel history, streamed under the poll's byte budget."""
    params = {"limit": limit, "offset": offset}
    async with client.stream("GET", path, params=params) as resp:
        body = await _read_capped(resp, budget)
        if resp.is_error:
            snippet = body[:200].decode(errors="replace")
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code} from {resp.url}; server said: {snippet}",
                request=resp.request, response=resp,
            )
    raw = json.loads(body)
    if not isinstance(raw, dict):
        return Page([], [], None)
    items = raw.get("messages")
    if not isinstance(items, list):
        items = []
    total = raw.get("total")
    if isinstance(total, bool) or not isinstance(total, int):
        total = None
    return Page([m for m in map(_normalize, items) if m], items, total)


def _messages_path(channel: str) -> str:
    return f"/api/channels/{quote(channel, safe='')}/messages"


def _page_key(items: list[Any]) -> tuple:
    """Identity of a page for spotting a server that ignores ``offset``."""
    def ident(item: Any) -> tuple:
        if not isinstance(item, dict):
            return (repr(item),)
        return tuple(str(item.get(k)) for k in ("packetId", "packetHash", "first_seen",
                                                 "sender", "text"))
    return (len(items), ident(items[0]), ident(items[-1]))


def _normalize(raw: Any) -> dict[str, Any] | None:
    """Map one CoreScope message to {sender, text, ts, first_seen}.

    ``ts`` is the radio's own send time when the packet carried one (what the
    mesh path sees too, so the two dedupe against each other) and the
    analyzer's first sighting otherwise — a message is still a message when
    the sending radio had no clock to stamp it with."""
    if not isinstance(raw, dict):
        return None
    text = raw.get("text")
    sender = raw.get("sender") or "unknown"
    first_seen = str(raw.get("first_seen") or raw.get("timestamp") or "")
    ts: Any = raw.get("sender_timestamp")
    if ts is None or isinstance(ts, bool):
        ts = _iso_epoch(first_seen)
    if not text or ts is None:
        return None
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return None
    return {
        "sender": str(sender),
        "text": str(text),
        "ts": ts,
        "first_seen": first_seen,
    }


def _iso_epoch(value: str) -> float | None:
    """Unix seconds for an ISO 8601 timestamp (``2026-07-06T17:00:05Z``), or
    None when it isn't one. A bare time is taken as UTC, which is what the
    analyzer writes."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


class _ByteBudget:
    """Bytes a poll may still read, across every page it fetches."""

    def __init__(self, limit: int):
        self.limit = limit
        self.remaining = limit

    def spend(self, n: int) -> bool:
        self.remaining -= n
        return self.remaining >= 0


async def _read_capped(resp: httpx.Response, budget: _ByteBudget) -> bytes:
    """The response body, or PollTooLarge once the poll passes its budget.
    Counts what arrives rather than trusting Content-Length, which a chunked
    response doesn't carry."""
    length = resp.headers.get("content-length", "")
    if length.isdigit() and int(length) > budget.remaining:
        raise PollTooLarge(
            f"{resp.url}: Content-Length {length} > {budget.remaining} left of {budget.limit}"
        )
    chunks: list[bytes] = []
    async for chunk in resp.aiter_bytes():
        if not budget.spend(len(chunk)):
            raise PollTooLarge(f"{resp.url}: poll passed {budget.limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


# -- Live check ----------------------------------------------------------------


@dataclass
class FeedProbe:
    """What one request to a feed came back with — the operator's view of
    "is this analyzer up, and does it still speak the API the poller
    expects?". Nothing here touches the archive."""

    name: str
    channel: str
    ok: bool = False
    error: str = ""
    elapsed_s: float = 0.0
    total: int | None = None          # messages the analyzer holds for the channel
    served: int = 0                   # entries on the newest page
    parsed: int = 0                   # of those, ones the poller would ingest
    fields: list[str] = field(default_factory=list)   # keys of the first entry served
    newest: list[dict[str, Any]] = field(default_factory=list)   # normalized, newest first
    channels: list[str] = field(default_factory=list)  # names the analyzer lists
    channels_error: str = ""          # why the listing is empty, when it failed

    @property
    def channel_listed(self) -> bool | None:
        """Whether the analyzer lists our channel; None when it wouldn't say."""
        if self.channels_error or not self.channels:
            return None
        return self.channel in self.channels


async def probe(
    client: httpx.AsyncClient, channel: str, *, name: str = "corescope", sample: int = 5
) -> FeedProbe:
    """Fetch the newest page of ``channel`` and the analyzer's channel list,
    and report what came back. ``ok`` means the page arrived and at least one
    entry parsed (or the channel is simply empty); any transport or shape
    failure lands in ``error`` rather than raising."""
    report = FeedProbe(name=name, channel=channel)
    started = time.monotonic()
    try:
        page = await _fetch_page(
            client, _messages_path(channel), PAGE_LIMIT, 0, _ByteBudget(MAX_POLL_BYTES)
        )
    except (httpx.HTTPError, PollTooLarge, ValueError) as exc:
        report.error = _describe(exc)
        report.elapsed_s = time.monotonic() - started
        return report
    report.elapsed_s = time.monotonic() - started
    report.total = page.total
    report.served = len(page.raw)
    report.parsed = len(page.messages)
    first = next((m for m in page.raw if isinstance(m, dict)), None)
    if first is not None:
        report.fields = list(first.keys())
    report.newest = sorted(page.messages, key=lambda m: m["first_seen"], reverse=True)[:sample]
    if page.raw and not page.messages:
        report.error = "the newest page parsed to nothing: no entry had text and a timestamp"
    else:
        report.ok = True

    try:
        resp = await client.get("/api/channels")
        resp.raise_for_status()
        listing = resp.json()
        entries = listing.get("channels") if isinstance(listing, dict) else None
        for entry in entries or []:
            if isinstance(entry, dict):
                label = entry.get("name") or entry.get("hash")
                if label:
                    report.channels.append(str(label))
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        report.channels_error = _describe(exc)
    return report


def _describe(exc: BaseException) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
