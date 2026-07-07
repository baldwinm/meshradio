"""Shared ingestion pipeline.

Both ingest paths (mesh serial, CoreScope poll) funnel every channel message
through ``IngestService.handle_message`` — one place for theme detection,
link extraction, dedupe, and event publication.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ..bus import EventBus, THEME_CREATED, TRACK_DISCOVERED
from ..db import Database
from . import parse

log = logging.getLogger(__name__)


class IngestService:
    def __init__(self, db: Database, bus: EventBus, channel: str, tz: str = "America/Chicago"):
        self.db = db
        self.bus = bus
        self.channel = channel
        self.tz = ZoneInfo(tz)

    def local_date(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(self.tz).strftime("%Y-%m-%d")

    async def handle_message(
        self,
        *,
        sender: str,
        text: str,
        ts: float,
        source: str,
        meta: dict | None = None,
    ) -> int:
        """Process one channel message. Returns number of new tracks inserted.

        ``meta`` (optional ``{"title", "artist", "duration"}``) seeds track
        metadata at insert time — the relay sends it so an embed-mode host
        never has to ask YouTube for what the home node already knows."""
        date = self.local_date(ts)

        theme_title = parse.parse_theme(text)
        if theme_title:
            theme = await self.db.create_theme(
                date, theme_title, set_by=sender, raw_message=text
            )
            log.info("theme for %s: %r (set by %s)", date, theme_title, sender)
            self.bus.publish(THEME_CREATED, {"theme": theme})

        links = parse.extract_links(text)
        if not links:
            return 0

        theme = await self.db.latest_theme_for_date(date)
        if theme is None:
            theme = await self.db.create_theme(date, parse.untitled_theme(date))
            self.bus.publish(THEME_CREATED, {"theme": theme})

        meta = meta or {}
        inserted = 0
        for link in links:
            track = await self.db.add_track(
                video_id=link.video_id,
                url=link.url,
                channel=self.channel,
                sender=sender,
                mesh_ts=ts,
                source=source,
                theme_id=theme["id"],
                title=meta.get("title"),
                artist=meta.get("artist"),
            )
            if track is None:
                log.debug("dedupe: %s from %s via %s already ingested", link.video_id, sender, source)
                if meta:
                    # Late-arriving metadata for a known track (e.g. the relay
                    # re-pushing history the receiver ingested bare): fill it
                    # in so embed hosts don't have to ask YouTube.
                    for row in await self.db.tracks_for_video(link.video_id):
                        await self.db.update_track_metadata(
                            row["id"],
                            title=meta.get("title"),
                            artist=meta.get("artist"),
                            duration=float(meta["duration"]) if meta.get("duration") else None,
                        )
                continue
            if meta.get("duration"):
                await self.db.update_track_metadata(track["id"], duration=float(meta["duration"]))
                track = await self.db.track_by_id(track["id"])
            inserted += 1
            log.info("new track %s from %s via %s", link.video_id, sender, source)
            self.bus.publish(TRACK_DISCOVERED, {"track": track})
        return inserted
