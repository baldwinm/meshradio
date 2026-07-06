"""CoreScope poller — fallback ingestion and first-boot backfill.

The AUS CoreScope API shape is not yet confirmed (architecture §6/§12.3), so
everything endpoint-specific is isolated in ``_fetch_messages`` and
``_normalize``. When the real API is known, this is the only file to touch.

The poller keeps a cursor (last seen message timestamp) in settings so
restarts don't re-fetch history; dedupe makes overlap harmless anyway.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from ..bus import EventBus, INGEST_STATUS
from ..config import CoreScopeConfig
from ..db import Database
from .service import IngestService

log = logging.getLogger(__name__)

CURSOR_KEY = "corescope.cursor"


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
            base_url=self.config.base_url, timeout=30
        ) as client:
            while True:
                try:
                    await self.poll_once(client)
                    self.bus.publish(INGEST_STATUS, {"corescope": "ok"})
                except Exception:
                    log.exception("CoreScope poll failed")
                    self.bus.publish(INGEST_STATUS, {"corescope": "error"})
                await asyncio.sleep(self.config.poll_interval_s)

    async def poll_once(self, client: httpx.AsyncClient) -> int:
        cursor = await self.db.get_setting(CURSOR_KEY)
        since = float(cursor) if cursor else None
        messages = await self._fetch_messages(client, since)
        inserted = 0
        newest = since or 0.0
        for msg in messages:
            inserted += await self.service.handle_message(
                sender=msg["sender"], text=msg["text"], ts=msg["ts"], source="corescope"
            )
            newest = max(newest, msg["ts"])
        if newest and newest != since:
            await self.db.set_setting(CURSOR_KEY, str(newest))
        if inserted:
            log.info("CoreScope poll: %d new tracks", inserted)
        return inserted

    # -- API adapter (the part that changes when the real API is confirmed) --

    async def _fetch_messages(
        self, client: httpx.AsyncClient, since: float | None
    ) -> list[dict[str, Any]]:
        """Fetch channel messages newer than ``since`` (unix seconds).

        PLACEHOLDER endpoint shape — confirm against the AUS CoreScope
        instance and adjust here only.
        """
        params: dict[str, Any] = {"channel": self.config.channel}
        if since:
            params["since"] = since
        resp = await client.get("/api/messages", params=params)
        resp.raise_for_status()
        return [m for m in map(self._normalize, resp.json()) if m]

    @staticmethod
    def _normalize(raw: dict[str, Any]) -> dict[str, Any] | None:
        """Map one raw API message to {sender, text, ts}. Tolerates a few
        plausible field spellings until the real schema is confirmed."""
        text = raw.get("text") or raw.get("message") or raw.get("payload")
        sender = raw.get("sender") or raw.get("from") or raw.get("origin") or "unknown"
        ts = raw.get("ts") or raw.get("timestamp") or raw.get("time")
        if not text or ts is None:
            return None
        return {"sender": str(sender), "text": str(text), "ts": float(ts)}
