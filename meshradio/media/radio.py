"""Radio mode: continue playback from a seed track using YouTube Mixes.

When the queue runs dry, a YouTube Mix ("radio") playlist for the current
track — ``watch?v=<id>&list=RD<id>`` — supplies related songs. yt-dlp
extracts the mix as a flat playlist (no downloads, no login), the entries
are inserted as ``source='radio'`` tracks with no theme (so they never
pollute the channel archive), and the normal discovered→cached→ready
pipeline takes over.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from ..bus import EventBus, TRACK_DISCOVERED
from ..config import CacheConfig
from ..db import Database

log = logging.getLogger(__name__)


class RadioService:
    def __init__(self, config: CacheConfig, db: Database, bus: EventBus):
        self.config = config
        self.db = db
        self.bus = bus

    async def extend(self, seed_track: dict[str, Any], limit: int = 10) -> int:
        """Queue up to ``limit`` related tracks for a seed. Returns how many
        new track rows were created (dedupe may reduce it)."""
        seed_id = seed_track["video_id"]
        entries = await self._fetch_mix(seed_id)
        if not entries:
            log.warning("radio: no mix entries for %s", seed_id)
            return 0

        # Skip songs already in the archive or a previous radio batch — a mix
        # frequently echoes recent channel picks back at us.
        inserted = 0
        now = time.time()
        for entry in entries:
            if inserted >= limit:
                break
            video_id = entry.get("id")
            if not video_id or video_id == seed_id:
                continue
            if await self.db.cached_track_for_video(video_id):
                continue
            track = await self.db.add_track(
                video_id=video_id,
                url=f"https://www.youtube.com/watch?v={video_id}",
                channel="radio",
                sender=f"radio · mix of {seed_track.get('title') or seed_id}",
                # Bucket the timestamp to the day: restarting radio from the
                # same seed on the same day dedupes instead of re-queueing.
                mesh_ts=float(int(now // 86400) * 86400),
                source="radio",
                theme_id=None,
                title=entry.get("title"),
                artist=entry.get("uploader") or entry.get("channel"),
            )
            if track is None:
                continue
            inserted += 1
            self.bus.publish(TRACK_DISCOVERED, {"track": track})
        log.info("radio: queued %d tracks from mix of %s", inserted, seed_id)
        return inserted

    async def _fetch_mix(self, video_id: str) -> list[dict[str, Any]]:
        """Flat-extract the YouTube Mix playlist for a video via yt-dlp."""
        url = f"https://www.youtube.com/watch?v={video_id}&list=RD{video_id}"
        cmd = [
            self.config.ytdlp_bin,
            "--flat-playlist",
            "--playlist-end", "25",
            "-J",
            *self.config.ytdlp_extra_args,
            "--", url,   # end of options: never treat the URL as a flag
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except FileNotFoundError:
            log.error("yt-dlp binary not found (%s); radio mode unavailable", self.config.ytdlp_bin)
            return []
        except asyncio.TimeoutError:
            log.error("yt-dlp mix extraction timed out for %s", video_id)
            return []
        if proc.returncode != 0:
            log.warning("mix extraction failed for %s: %s", video_id, stderr.decode(errors="replace")[-300:])
            return []
        try:
            data = json.loads(stdout.decode(errors="replace"))
        except json.JSONDecodeError:
            return []
        return list(data.get("entries") or [])
