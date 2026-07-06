"""Cache-first downloader.

On ``track.discovered``: run yt-dlp into the cache dir, update track metadata
from the extractor JSON, publish ``track.ready``. The player only ever plays
local files (architecture §7).

Fallback ladder on failure: retry with backoff → oEmbed metadata-only
(``track.failed``, archive stays browsable with a "couldn't fetch audio"
badge). yt-dlp runs as a subprocess so an extractor crash can't take the
radio down with it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from ..bus import EventBus, TRACK_DISCOVERED, TRACK_FAILED, TRACK_READY
from ..config import CacheConfig
from ..db import Database
from . import metadata

log = logging.getLogger(__name__)


class Cacher:
    def __init__(self, config: CacheConfig, cache_dir: Path, db: Database, bus: EventBus):
        self.config = config
        self.cache_dir = cache_dir
        self.db = db
        self.bus = bus
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._task = asyncio.create_task(self._run(), name="cacher")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        sub = self.bus.subscribe(TRACK_DISCOVERED)
        try:
            # Startup: resume anything that was pending when we last shut down.
            for track in await self.db.pending_tracks():
                await self.process_track(track)
            async for _topic, payload in sub:
                await self.process_track(payload["track"])
        finally:
            sub.close()

    async def process_track(self, track: dict[str, Any]) -> None:
        track_id = track["id"]
        video_id = track["video_id"]

        # Same song already cached under another track row? Reuse the file.
        existing = await self.db.cached_track_for_video(video_id)
        if existing and existing["id"] != track_id and Path(existing["cache_path"]).exists():
            await self.db.update_track_metadata(
                track_id,
                title=existing["title"],
                artist=existing["artist"],
                duration=existing["duration"],
            )
            await self.db.set_cache_status(track_id, "ready", existing["cache_path"])
            self.bus.publish(TRACK_READY, {"track": await self.db.track_by_id(track_id)})
            return

        for attempt in range(self.config.max_retries):
            info = await self._download(track["url"], video_id)
            if info is not None:
                await self.db.update_track_metadata(
                    track_id,
                    title=info.get("title"),
                    artist=info.get("artist") or info.get("uploader"),
                    duration=info.get("duration"),
                )
                await self.db.set_cache_status(track_id, "ready", str(info["_filepath"]))
                self.bus.publish(TRACK_READY, {"track": await self.db.track_by_id(track_id)})
                await self.prune()
                return
            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(self.config.retry_backoff_s * (attempt + 1))

        # Metadata-only mode: no audio, but the archive entry stays intact.
        log.warning("giving up on audio for %s; falling back to metadata-only", video_id)
        meta = await metadata.fetch_oembed(video_id)
        if meta:
            await self.db.update_track_metadata(
                track_id, title=meta["title"] or None, artist=meta["artist"] or None
            )
        await self.db.set_cache_status(track_id, "failed")
        self.bus.publish(TRACK_FAILED, {"track": await self.db.track_by_id(track_id)})

    async def _download(self, url: str, video_id: str) -> dict[str, Any] | None:
        """Run yt-dlp; return its info JSON (plus _filepath) or None on failure."""
        target = self.cache_dir / f"{video_id}.{self.config.audio_format}"
        if target.exists():
            info = {"_filepath": target}
            return info
        cmd = [
            self.config.ytdlp_bin,
            "-f", "bestaudio",
            "-x", "--audio-format", self.config.audio_format,
            "--no-playlist",
            "--print-json",
            "-o", str(self.cache_dir / "%(id)s.%(ext)s"),
            url,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except FileNotFoundError:
            log.error("yt-dlp binary not found (%s)", self.config.ytdlp_bin)
            return None
        except asyncio.TimeoutError:
            log.error("yt-dlp timed out for %s", video_id)
            return None
        if proc.returncode != 0:
            log.warning("yt-dlp failed for %s: %s", video_id, stderr.decode(errors="replace")[-500:])
            return None
        try:
            info: dict[str, Any] = json.loads(stdout.decode(errors="replace").strip().splitlines()[-1])
        except (json.JSONDecodeError, IndexError):
            info = {}
        if not target.exists():
            log.warning("yt-dlp succeeded but %s missing", target)
            return None
        info["_filepath"] = target
        return info

    async def prune(self) -> None:
        """LRU-prune the cache back under the size cap. Pruned tracks return to
        'pending' so they can be re-fetched on demand later."""
        total = sum(f.stat().st_size for f in self.cache_dir.iterdir() if f.is_file())
        if total <= self.config.max_bytes:
            return
        for track in await self.db.cached_tracks_lru():
            if total <= self.config.max_bytes:
                break
            path = Path(track["cache_path"])
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                total -= size
            await self.db.set_cache_status(track["id"], "pending")
            log.info("pruned %s from cache", track["video_id"])
