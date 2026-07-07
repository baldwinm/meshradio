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
from ..runtime import Service
from . import metadata

log = logging.getLogger(__name__)


class Cacher(Service):
    def __init__(
        self,
        config: CacheConfig,
        cache_dir: Path,
        db: Database,
        bus: EventBus,
        embed: bool = False,
    ):
        self.config = config
        self.cache_dir = cache_dir
        self.db = db
        self.bus = bus
        # Embed mode (public hosting): never download audio — the browser
        # streams from YouTube directly. Tracks go straight to 'ready' with
        # oEmbed metadata and no cache file.
        self.embed = embed
        self._embed_attempts: dict[int, int] = {}

    def start(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        super().start()

    async def _run(self) -> None:
        sub = self.bus.subscribe(TRACK_DISCOVERED)
        try:
            while True:
                # Sweep pending rows every cycle, not just at startup: this
                # catches boot backlog, bus events dropped under a relay
                # backfill burst, and embed-mode oEmbed retries. Anything
                # that leaves a track pending self-heals within a minute.
                for track in await self.db.pending_tracks():
                    await self._process_safely(track)
                try:
                    _topic, payload = await asyncio.wait_for(sub.get(), timeout=60)
                except asyncio.TimeoutError:
                    continue
                await self._process_safely(payload["track"])
        finally:
            sub.close()

    async def _process_safely(self, track: dict[str, Any]) -> None:
        """One bad track must not kill the cacher loop for all that follow."""
        try:
            await self.process_track(track)
        except Exception:
            log.exception("cacher: unexpected error on %s", track.get("video_id"))

    async def process_track(self, track: dict[str, Any]) -> None:
        track_id = track["id"]
        video_id = track["video_id"]

        # The sweep and the event stream can hand us the same track (or a
        # stale snapshot of one); only pending rows need work.
        current = await self.db.track_by_id(track_id)
        if current is None or current["cache_status"] != "pending":
            return

        if self.embed:
            # Relayed tracks arrive with metadata already attached — nothing
            # to look up, and no dependency on YouTube answering this host.
            if current.get("title"):
                await self.db.set_cache_status(track_id, "ready")
                self.bus.publish(TRACK_READY, {"track": await self.db.track_by_id(track_id)})
                return
            meta = await metadata.fetch_oembed(video_id)
            if meta is None:
                # Could be a deleted video or a transient throttle; stay
                # pending so the sweep retries, fail only after max_retries.
                attempts = self._embed_attempts.get(track_id, 0) + 1
                self._embed_attempts[track_id] = attempts
                if attempts < self.config.max_retries:
                    log.warning(
                        "oEmbed failed for %s (attempt %d/%d); will retry",
                        video_id, attempts, self.config.max_retries,
                    )
                    return
                self._embed_attempts.pop(track_id, None)
                await self.db.set_cache_status(track_id, "failed")
                self.bus.publish(TRACK_FAILED, {"track": await self.db.track_by_id(track_id)})
                return
            self._embed_attempts.pop(track_id, None)
            await self.db.update_track_metadata(
                track_id, title=meta["title"] or None, artist=meta["artist"] or None
            )
            await self.db.set_cache_status(track_id, "ready")
            self.bus.publish(TRACK_READY, {"track": await self.db.track_by_id(track_id)})
            return

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
        ]
        if self.config.ffmpeg_location:
            cmd += ["--ffmpeg-location", self.config.ffmpeg_location]
        cmd += list(self.config.ytdlp_extra_args)
        cmd.append(url)
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
