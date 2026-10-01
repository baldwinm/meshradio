"""Shared ingestion pipeline.

Both ingest paths (mesh serial, CoreScope poll) funnel every channel message
through ``IngestService.handle_message`` — one place for theme detection,
link extraction, dedupe, and event publication.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from ..bus import THEME_CREATED, TRACK_DISCOVERED, EventBus
from ..db import MAX_SENDER, MAX_TITLE, Database, clean_duration, clean_text
from . import parse

log = logging.getLogger(__name__)


class IngestService:
    def __init__(self, db: Database, bus: EventBus, channel: str, tz: str = "America/Chicago"):
        self.db = db
        self.bus = bus
        self.channel = channel
        self.tz = ZoneInfo(tz)

    def local_date(self, ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=UTC).astimezone(self.tz).strftime("%Y-%m-%d")

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
        # Channel messages are short; anything huge is hostile or corrupt.
        # Cap before regex work (mesh RF, CoreScope, and relay all land here).
        # The sender is bounded the same way the archive bounds it, up front,
        # so the log lines below carry the name the rows will.
        text = str(text or "")[:4096]
        sender = clean_text(sender, MAX_SENDER) or "unknown"
        date = self.local_date(ts)

        theme = None
        theme_title = clean_text(parse.parse_theme(text), MAX_TITLE)
        if theme_title:
            existing = await self.db.latest_theme_for_date(date)
            if existing is None:
                # First theme of the day — set and lock it.
                theme = await self.db.create_theme(
                    date, theme_title, set_by=sender, raw_message=text, locked=True
                )
                log.info("theme for %s: %r (set by %s)", date, theme_title, sender)
                self.bus.publish(THEME_CREATED, {"theme": theme})
            elif existing["locked"]:
                # The theme was set in the morning and is locked. A later
                # "Theme: …" message must not reset it or spawn a rival
                # playlist — keep ingesting links under the existing theme.
                log.info(
                    "theme for %s already locked (%r); ignoring reset to %r by %s",
                    date, existing["title"], theme_title, sender,
                )
                theme = existing
            else:
                # Links arrived before the theme, so an "Untitled —"
                # placeholder holds them. Adopt the real title into it (one
                # playlist for the day) and lock it.
                theme = await self.db.adopt_theme(
                    existing["id"], theme_title, set_by=sender, raw_message=text
                )
                log.info(
                    "theme for %s: %r (set by %s, adopted placeholder)",
                    date, theme_title, sender,
                )
                self.bus.publish(THEME_CREATED, {"theme": theme})

        links = parse.extract_links(text)
        if not links:
            return 0

        if theme is None:
            theme = await self.db.latest_theme_for_date(date)
        if theme is None:
            theme = await self.db.create_theme(date, parse.untitled_theme(date))
            self.bus.publish(THEME_CREATED, {"theme": theme})

        # The relay's metadata is only as good as the node that sent it. Title
        # and artist are bounded where they're stored (Database.add_track,
        # update_track_metadata); the duration is checked here so a value that
        # isn't one is simply not supplied, never written.
        meta = meta or {}
        duration = clean_duration(meta.get("duration"))
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
                log.debug(
                    "dedupe: %s from %s via %s already ingested", link.video_id, sender, source
                )
                if meta:
                    # Late-arriving metadata for a known track (e.g. the relay
                    # re-pushing history the receiver ingested bare): fill it
                    # in so embed hosts don't have to ask YouTube.
                    for row in await self.db.tracks_for_video(link.video_id):
                        await self.db.update_track_metadata(
                            row["id"],
                            title=meta.get("title"),
                            artist=meta.get("artist"),
                            duration=duration,
                        )
                continue
            if duration is not None:
                await self.db.update_track_metadata(track["id"], duration=duration)
                track = await self.db.track_by_id(track["id"])
            inserted += 1
            log.info("new track %s from %s via %s", link.video_id, sender, source)
            self.bus.publish(TRACK_DISCOVERED, {"track": track})
        return inserted
