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

The LetsMesh MeshCore analyzer (analyzer.letsmesh.net) is the same API by the
same author, so it runs through this exact poller as a backup feed — a second
instance with its own ``name`` (cursor key, status field) and ``source``.
Because dedupe keys on channel+sender+video+minute, not on source, the two
feeds no-op each other's overlap; the backup only adds rows CoreScope missed.
"""

from __future__ import annotations

import asyncio
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
        # second, identically-shaped feed (the LetsMesh backup) doesn't clobber
        # the primary's cursor. ``source`` is the provenance stamped on tracks.
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
                    # Body snippet tells a Cloudflare block apart from an
                    # origin error without dumping a whole challenge page.
                    log.error(
                        "%s poll: HTTP %d from %s; server said: %.200s",
                        self.name,
                        exc.response.status_code,
                        exc.request.url,
                        exc.response.text,
                    )
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
        for msg in fresh:
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
        """Fetch the channel's full message history."""
        resp = await client.get(f"/api/channels/{quote(self.config.channel, safe='')}/messages")
        resp.raise_for_status()
        raw = resp.json()
        return [m for m in map(self._normalize, raw.get("messages", [])) if m]

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
        """Map one CoreScope message to {sender, text, ts, first_seen}."""
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
