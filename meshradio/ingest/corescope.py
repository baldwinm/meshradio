"""CoreScope poller — fallback ingestion and first-boot backfill.

Written against CoreScope's real API (github.com/Kpa-clawbot/CoreScope,
verified 2026-07 against a live instance):

    GET /api/channels/{hash}/messages -> {"messages": [...], "total": N}

where ``hash`` is the URL-encoded channel name (``#music`` -> ``%23music``)
and each message carries ``sender``, ``text``, ``sender_timestamp`` (unix
seconds, the mesh-side send time — the same value the local node sees, which
is what makes cross-source dedupe line up) and ``first_seen`` (ISO, when the
CoreScope server first observed the packet).

There is no ``since`` parameter: the server returns full channel history,
which doubles as the first-boot backfill. The poller keeps a cursor on
``first_seen`` in settings so steady-state polls skip already-processed
messages; late-arriving RF duplicates and cursor ties fall through to the
dedupe hash, which makes reprocessing a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote

import httpx

from .. import __version__
from ..bus import EventBus, INGEST_STATUS
from ..config import CoreScopeConfig
from ..db import Database
from .service import IngestService

log = logging.getLogger(__name__)

CURSOR_KEY = "corescope.cursor"

# Descriptive UA: httpx's default ("python-httpx/x.y") plus a datacenter IP
# reads as a bot to Cloudflare and gets 403'd on hosted deployments.
USER_AGENT = f"meshradio/{__version__} (+https://github.com/baldwinm/meshradio)"


class CoreScopePoller:
    def __init__(
        self,
        config: CoreScopeConfig,
        service: IngestService,
        db: Database,
        bus: EventBus,
    ):
        self.config = config
        self.service = service
        self.db = db
        self.bus = bus
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="corescope-poller")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        if not self.config.base_url:
            log.warning("CoreScope base_url not configured; poller idle")
            self.bus.publish(INGEST_STATUS, {"corescope": "unconfigured"})
            return
        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            timeout=30,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            while True:
                try:
                    await self.poll_once(client)
                    self.bus.publish(INGEST_STATUS, {"corescope": "ok"})
                except httpx.HTTPStatusError as exc:
                    # Body snippet tells a Cloudflare block apart from an
                    # origin error without dumping a whole challenge page.
                    log.error(
                        "CoreScope poll: HTTP %d from %s; server said: %.200s",
                        exc.response.status_code,
                        exc.request.url,
                        exc.response.text,
                    )
                    self.bus.publish(INGEST_STATUS, {"corescope": "error"})
                except Exception:
                    log.exception("CoreScope poll failed")
                    self.bus.publish(INGEST_STATUS, {"corescope": "error"})
                await asyncio.sleep(self.config.poll_interval_s)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        cursor = await self.db.get_setting(CURSOR_KEY, "")
        messages = await self._fetch_messages(client)
        # Skip what previous polls handled; include cursor ties (dedupe
        # no-ops them) so nothing sharing a first_seen second is lost.
        fresh = [m for m in messages if not cursor or m["first_seen"] >= cursor]
        # Themes must land before the links posted after them.
        fresh.sort(key=lambda m: m["ts"])
        inserted = 0
        for msg in fresh:
            inserted += await self.service.handle_message(
                sender=msg["sender"], text=msg["text"], ts=msg["ts"], source="corescope"
            )
        newest = max((m["first_seen"] for m in fresh), default="")
        if newest and newest != cursor:
            await self.db.set_setting(CURSOR_KEY, newest)
        if inserted:
            log.info("CoreScope poll: %d new tracks", inserted)
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
