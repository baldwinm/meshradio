"""Cache-first downloader.

On ``track.discovered``: run yt-dlp into the cache dir, update track metadata
from the extractor JSON, publish ``track.ready``. The player only ever plays
local files (architecture §7).

Fallback ladder on failure: retry with backoff → oEmbed metadata-only
(``track.failed``, archive stays browsable with a "couldn't fetch audio"
badge). yt-dlp runs as a subprocess so an extractor crash can't take the
radio down with it.

A few tracks are worked at once (``[cache] concurrency``), each in its own
task: one download that yt-dlp sits on for five minutes used to hold up
every track behind it in the backlog.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from ..bus import EventBus, TRACK_DISCOVERED, TRACK_FAILED, TRACK_READY
from ..config import CacheConfig
from ..db import Database
from ..net import http_client
from ..runtime import Service, spawn
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
        # Running estimate of cache-dir bytes, seeded once from disk off the
        # event loop. Lets prune() skip the full walk while well under the cap.
        self._cache_bytes: int | None = None
        self._prune_lock = asyncio.Lock()      # two workers finishing at once prune once
        # Worker bookkeeping (see _run): one task per track in flight, capped
        # by config.concurrency. A track is never worked twice at once even
        # though the sweep and the event stream both hand it over.
        self._inflight: dict[int, asyncio.Task] = {}
        self._slots: asyncio.Semaphore | None = None
        # One HTTP client for every oEmbed lookup while running (a fresh TLS
        # handshake per video was most of each lookup's time). None outside
        # _run — process_track then lets metadata use a throwaway client.
        self._http: httpx.AsyncClient | None = None

    def start(self) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        super().start()

    async def _run(self) -> None:
        sub = self.bus.subscribe(TRACK_DISCOVERED)
        self._slots = asyncio.Semaphore(max(1, int(self.config.concurrency)))
        try:
            async with http_client(timeout=15) as self._http:
                while True:
                    # Sweep pending rows every cycle, not just at startup: this
                    # catches boot backlog, bus events dropped under a relay
                    # backfill burst, and embed-mode oEmbed retries. Anything
                    # that leaves a track pending self-heals within a minute.
                    for track in await self.db.pending_tracks():
                        await self._submit(track)
                    try:
                        _topic, payload = await asyncio.wait_for(sub.get(), timeout=60)
                    except asyncio.TimeoutError:
                        continue
                    await self._submit(payload["track"])
        finally:
            sub.close()
            self._http = None
            for task in list(self._inflight.values()):
                task.cancel()

    async def _submit(self, track: dict[str, Any]) -> None:
        """Hand a track to a worker, waiting for a free slot. Blocking here
        is deliberate: with every worker busy the loop stops pulling events,
        the bus buffers them, and the next sweep picks up anything dropped."""
        track_id = track["id"]
        if track_id in self._inflight:
            return
        assert self._slots is not None
        await self._slots.acquire()
        self._inflight[track_id] = spawn(f"cache-{track_id}", self._work(track))

    async def _work(self, track: dict[str, Any]) -> None:
        try:
            await self._process_safely(track)
        finally:
            self._inflight.pop(track["id"], None)
            assert self._slots is not None
            self._slots.release()

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
            meta = await metadata.fetch_oembed(video_id, self._http)
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
                await self.prune(added_bytes=int(info.get("_filesize", 0)))
                return
            if attempt < self.config.max_retries - 1:
                await asyncio.sleep(self.config.retry_backoff_s * (attempt + 1))

        # Metadata-only mode: no audio, but the archive entry stays intact.
        log.warning("giving up on audio for %s; falling back to metadata-only", video_id)
        meta = await metadata.fetch_oembed(video_id, self._http)
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
            return {"_filepath": target, "_filesize": target.stat().st_size}
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
        cmd += ["--", url]   # end of options: never treat the URL as a flag
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            log.error("yt-dlp binary not found (%s)", self.config.ytdlp_bin)
            return None
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except asyncio.TimeoutError:
            # wait_for only abandons the await; the process itself would
            # keep downloading (and holding a slot's worth of CPU) forever.
            _kill(proc)
            log.error("yt-dlp timed out for %s", video_id)
            return None
        except asyncio.CancelledError:
            _kill(proc)               # shutdown: no orphaned yt-dlp
            raise
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
        info["_filesize"] = target.stat().st_size
        return info

    def _dir_size(self) -> int:
        """Total bytes of cache files. Walks the dir with blocking stat calls —
        always run this off the event loop via ``asyncio.to_thread``."""
        return sum(f.stat().st_size for f in self.cache_dir.iterdir() if f.is_file())

    async def prune(self, added_bytes: int = 0) -> None:
        """LRU-prune the cache back under the size cap. Pruned tracks return to
        'pending' so they can be re-fetched on demand later.

        The full cache dir is stat-walked only when a running byte estimate
        (seeded once from disk, always off the event loop) says we've crossed
        the cap — so the common under-cap case, hit after every download, no
        longer blocks the loop stat-ing hundreds of files."""
        async with self._prune_lock:
            if self._cache_bytes is None:
                # The seed walk already reflects the file just written, so don't
                # also add its bytes; accumulate added_bytes only on later calls.
                self._cache_bytes = await asyncio.to_thread(self._dir_size)
            else:
                self._cache_bytes += added_bytes
            if self._cache_bytes <= self.config.max_bytes:
                return
            total = await asyncio.to_thread(self._dir_size)  # authoritative before evicting
            # A batch of candidates at a time: being over the cap is usually
            # a matter of a file or two, and marking a track pending takes it
            # out of the next batch, so the loop always makes progress.
            while total > self.config.max_bytes:
                batch = await self.db.cached_tracks_lru(limit=50)
                if not batch:
                    break
                for track in batch:
                    if total <= self.config.max_bytes:
                        break
                    path = Path(track["cache_path"])
                    if path.exists():
                        size = path.stat().st_size
                        path.unlink()
                        total -= size
                    await self.db.set_cache_status(track["id"], "pending")
                    log.info("pruned %s from cache", track["video_id"])
            self._cache_bytes = total


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
