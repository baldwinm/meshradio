"""CoreScope-compatible analyzer poller — fallback ingestion and first-boot
backfill.

Written against CoreScope's real API (github.com/Kpa-clawbot/CoreScope,
verified 2026-07 against a live instance):

    GET /api/channels/{hash}/messages -> {"messages": [...], "total": N}

where ``hash`` is the URL-encoded channel name (``#music`` -> ``%23music``)
and each message carries ``sender``, ``text``, ``sender_timestamp`` (unix
seconds, the mesh-side send time — the same value the local node sees, which
is what makes cross-source dedupe line up) and ``first_seen`` (ISO, when the
server first observed the packet).

There is no ``since`` parameter: the server returns full channel history,
which doubles as the first-boot backfill. The poller keeps a cursor on
``first_seen`` in settings so steady-state polls skip already-processed
messages; late-arriving RF duplicates and cursor ties fall through to the
dedupe hash, which makes reprocessing a no-op.

The ``name``/``source`` params keep the poller reusable for any additional
CoreScope-compatible feed (its own cursor key, status field, and track
provenance); because dedupe keys on channel+sender+video+minute rather than
source, two such feeds no-op each other's overlap. app.py uses that to run
the ``comchan`` backup feed (analyzer.comchan.net) alongside the primary —
concurrently, not failed over to, so neither instance's outage stalls
ingestion and neither needs health tracking to decide who is in charge.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

from ..bus import EventBus, INGEST_STATUS
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

# The API returns the channel's whole history every poll (no ``since``). A
# year of a busy channel is a few megabytes; this is the point past which
# the response is not a channel any more, and reading on would only be
# growing our memory at the analyzer's say-so.
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
                    # The message carries a body snippet (see _fetch_messages):
                    # it tells a Cloudflare block apart from an origin error
                    # without dumping a whole challenge page.
                    log.error("%s poll: %s", self.name, exc)
                    self.bus.publish(INGEST_STATUS, {self.name: "error"})
                except Exception:
                    log.exception("%s poll failed", self.name)
                    self.bus.publish(INGEST_STATUS, {self.name: "error"})
                await asyncio.sleep(self.config.poll_interval_s)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        cursor = await self.db.get_setting(self.cursor_key, "")
        messages = await self._fetch_messages(client)
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

    # -- API adapter -----------------------------------------------------------

    async def _fetch_messages(self, client: httpx.AsyncClient) -> list[dict[str, Any]]:
        """Fetch the channel's full message history, streamed under a size cap."""
        path = f"/api/channels/{quote(self.config.channel, safe='')}/messages"
        async with client.stream("GET", path) as resp:
            body = await _read_capped(resp, MAX_POLL_BYTES)
            if resp.is_error:
                snippet = body[:200].decode(errors="replace")
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} from {resp.url}; server said: {snippet}",
                    request=resp.request, response=resp,
                )
        raw = json.loads(body)
        if not isinstance(raw, dict):
            return []
        return [m for m in map(self._normalize, raw.get("messages") or []) if m]

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
        """Map one CoreScope message to {sender, text, ts, first_seen}."""
        if not isinstance(raw, dict):
            return None
        text = raw.get("text")
        sender = raw.get("sender") or "unknown"
        ts = raw.get("sender_timestamp")
        if not text or ts is None:
            return None
        return {
            "sender": str(sender),
            "text": str(text),
            "ts": float(ts),
            "first_seen": str(raw.get("first_seen") or ""),
        }


async def _read_capped(resp: httpx.Response, limit: int) -> bytes:
    """The response body, or PollTooLarge once it passes ``limit``. Counts what
    arrives rather than trusting Content-Length, which a chunked response
    doesn't carry."""
    length = resp.headers.get("content-length", "")
    if length.isdigit() and int(length) > limit:
        raise PollTooLarge(f"{resp.url}: Content-Length {length} > {limit}")
    chunks: list[bytes] = []
    size = 0
    async for chunk in resp.aiter_bytes():
        size += len(chunk)
        if size > limit:
            raise PollTooLarge(f"{resp.url}: body passed {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)
