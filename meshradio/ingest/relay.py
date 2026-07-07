"""Relay pusher — mirror this node's channel history to a hosted instance.

Cloudflare challenges datacenter IPs, so a hosted (embed-mode) deployment
can't poll CoreScope itself. This service runs on a node with residential
internet (the Pi at home), reconstructs channel messages from the local DB,
and POSTs anything new to the hosted instance's authenticated
``/api/ingest`` endpoint. The receiver funnels them through the normal
ingest pipeline, whose dedupe makes re-pushes no-ops, so the cursor here is
an optimization rather than a correctness requirement.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from ..config import RelayConfig
from ..db import Database
from ..net import http_client
from ..runtime import Service

log = logging.getLogger(__name__)

CURSOR_KEY = "relay.cursor"


class RelayPusher(Service):
    def __init__(self, config: RelayConfig, db: Database, tz: str = "America/Chicago"):
        self.config = config
        self.db = db
        self.tz = ZoneInfo(tz)

    async def _run(self) -> None:
        async with http_client(
            timeout=60,
            headers={"Authorization": f"Bearer {self.config.token}"},
        ) as client:
            while True:
                try:
                    await self.push_once(client)
                except httpx.HTTPStatusError as exc:
                    log.error(
                        "relay push: HTTP %d from %s; server said: %.200s",
                        exc.response.status_code,
                        exc.request.url,
                        exc.response.text,
                    )
                except Exception:
                    log.exception("relay push failed")
                await asyncio.sleep(self.config.interval_s)

    async def push_once(self, client: httpx.AsyncClient) -> int:
        """Push new messages; an empty batch still POSTs as a heartbeat so a
        wiped receiver (ephemeral hosting resets its disk on deploys and
        spin-downs) is detected by track-count mismatch and re-backfilled."""
        cursor = await self.db.get_setting(CURSOR_KEY, "")
        messages, newest = await self.collect(cursor)
        resp = await client.post(
            self.config.push_url.rstrip("/") + "/api/ingest",
            json={"messages": messages},
        )
        resp.raise_for_status()
        data = resp.json()
        # Advance only after a confirmed 2xx so a failed push retries in full.
        if newest != cursor:
            await self.db.set_setting(CURSOR_KEY, newest)
        if messages:
            log.info(
                "relay: pushed %d messages (%s new remotely)",
                len(messages), data.get("inserted", "?"),
            )
        remote_total = data.get("tracks")
        local_total = await self.db.channel_track_count()
        if remote_total is not None and remote_total < local_total:
            log.warning(
                "relay: receiver has %d tracks vs %d local — wiped? re-backfilling",
                remote_total, local_total,
            )
            await self.db.set_setting(CURSOR_KEY, "")
            if cursor:  # just re-pushed from scratch? then don't loop on a
                return await self.push_once(client)  # persistent mismatch
        return len(messages)

    @staticmethod
    def _parse_cursor(raw: str) -> dict[str, list]:
        """Cursor = per-table (timestamp, id) pairs, JSON-encoded. Legacy
        plain-timestamp cursors from earlier versions parse as (ts, 0)."""
        if raw:
            try:
                parsed = json.loads(raw)
                return {
                    "themes": list(parsed["themes"]),
                    "tracks": list(parsed["tracks"]),
                }
            except (ValueError, KeyError, TypeError):
                return {"themes": [raw, 0], "tracks": [raw, 0]}
        return {"themes": ["", 0], "tracks": ["", 0]}

    async def collect(self, cursor: str) -> tuple[list[dict[str, Any]], str]:
        """Reconstruct channel messages for everything ingested after
        ``cursor``. Returns (messages sorted by mesh time, newest cursor)."""
        cur = self._parse_cursor(cursor)
        themes = await self.db.themes_since(*cur["themes"])
        tracks = await self.db.tracks_since(*cur["tracks"])
        messages: list[dict[str, Any]] = []
        for theme in themes:
            # Auto-created placeholder themes carry no message; the receiver
            # regenerates its own when the day's first link arrives.
            if not theme.get("raw_message") and not theme.get("set_by"):
                continue
            messages.append({
                "sender": theme.get("set_by") or "mesh",
                "text": theme.get("raw_message") or f"Theme: {theme['title']}",
                "ts": self._theme_ts(theme["date"]),
            })
        for track in tracks:
            # Ship the metadata we already have: the receiver (embed mode on
            # a datacenter IP) may not be able to ask YouTube itself.
            meta = {
                k: track[k] for k in ("title", "artist", "duration") if track.get(k)
            }
            messages.append({
                "sender": track["sender"],
                "text": track["url"],
                "ts": float(track["mesh_ts"] or 0),
                **({"meta": meta} if meta else {}),
            })
        messages.sort(key=lambda m: m["ts"])
        newest = json.dumps({
            "themes": [themes[-1]["created_at"], themes[-1]["id"]] if themes else cur["themes"],
            "tracks": [tracks[-1]["ingested_at"], tracks[-1]["id"]] if tracks else cur["tracks"],
        })
        return messages, newest

    def _theme_ts(self, date: str) -> float:
        """Just past local midnight, so the theme lands on the right day and
        precedes every track posted that day."""
        dt = datetime.strptime(date, "%Y-%m-%d").replace(hour=0, minute=5, tzinfo=self.tz)
        return dt.timestamp()
