"""Player service: queue + live-mode policy on top of a swappable backend.

Live mode policy (architecture §7, locked): a new track never interrupts the
current one. Idle in Live mode → auto-play; busy → enqueue; quiet hours
suppress auto-play. mpv does the actual decoding; a NullBackend keeps the
whole service testable and runnable on machines without libmpv.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from ..bus import EventBus, PLAYER_STATE, TRACK_READY
from ..config import PlayerConfig
from ..db import Database

log = logging.getLogger(__name__)


class NullBackend:
    """Simulated playback for dev machines and tests: 'plays' a track for its
    duration (or a few seconds if unknown), then fires on_end."""

    def __init__(self, time_scale: float = 1.0):
        self.on_end: Callable[[], None] | None = None
        self.time_scale = time_scale
        self.volume = 100
        self.paused = False
        self._timer: asyncio.Task | None = None

    async def play(self, path: str, duration: float | None) -> None:
        await self.stop()
        self.paused = False
        wait = (duration or 3.0) * self.time_scale
        self._timer = asyncio.create_task(self._finish_after(wait))

    async def _finish_after(self, wait: float) -> None:
        await asyncio.sleep(wait)
        if self.on_end:
            self.on_end()

    async def stop(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def set_volume(self, volume: int) -> None:
        self.volume = volume


class MpvBackend:
    """mpv via python-mpv JSON IPC. Outputs to the current PipeWire default
    sink, so output switching needs zero player logic."""

    def __init__(self) -> None:
        import mpv  # deferred: needs libmpv, present only on the appliance

        self._loop = asyncio.get_event_loop()
        self.on_end: Callable[[], None] | None = None
        self._player = mpv.MPV(video=False, terminal=False)
        self._player.observe_property("eof-reached", self._on_eof)

    def _on_eof(self, _name: str, value: Any) -> None:
        if value and self.on_end:
            self._loop.call_soon_threadsafe(self.on_end)

    async def play(self, path: str, duration: float | None) -> None:
        self._player.pause = False
        self._player.play(path)

    async def stop(self) -> None:
        self._player.stop()

    async def pause(self) -> None:
        self._player.pause = True

    async def resume(self) -> None:
        self._player.pause = False

    async def set_volume(self, volume: int) -> None:
        self._player.volume = volume


class PlayerService:
    def __init__(
        self,
        config: PlayerConfig,
        db: Database,
        bus: EventBus,
        backend: NullBackend | MpvBackend,
        output_getter: Callable[[], str] = lambda: "speaker",
    ):
        self.config = config
        self.db = db
        self.bus = bus
        self.backend = backend
        self.backend.on_end = self._schedule_track_ended
        self.output_getter = output_getter
        self.tz = ZoneInfo(config.timezone)

        self.status: str = "idle"          # idle | playing | paused
        self.mode: str = "live"            # live | archive
        self.current: dict[str, Any] | None = None
        self.queue: list[dict[str, Any]] = []
        self.volume: int = config.volume
        self._play_id: int | None = None
        self._task: asyncio.Task | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="player")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self.backend.stop()

    async def _run(self) -> None:
        await self.backend.set_volume(self.volume)
        self.publish_state()
        sub = self.bus.subscribe(TRACK_READY)
        try:
            async for _topic, payload in sub:
                await self.on_track_ready(payload["track"])
        finally:
            sub.close()

    # -- live policy -----------------------------------------------------------

    async def on_track_ready(self, track: dict[str, Any]) -> None:
        if (
            self.status == "idle"
            and self.mode == "live"
            and self.config.live_autoplay
            and not self.in_quiet_hours()
        ):
            await self.play_track(track)
        else:
            self.queue.append(track)
            self.publish_state()

    def in_quiet_hours(self, now: datetime | None = None) -> bool:
        spec = self.config.quiet_hours
        if not spec or "-" not in spec:
            return False
        try:
            start_s, end_s = spec.split("-")
            start = datetime.strptime(start_s.strip(), "%H:%M").time()
            end = datetime.strptime(end_s.strip(), "%H:%M").time()
        except ValueError:
            log.warning("bad quiet_hours spec %r", spec)
            return False
        current = (now or datetime.now(self.tz)).time()
        if start <= end:
            return start <= current < end
        return current >= start or current < end  # overnight span

    # -- playback commands -------------------------------------------------

    async def play_track(self, track: dict[str, Any]) -> None:
        if not track.get("cache_path"):
            log.warning("track %s has no cached audio; skipping", track.get("video_id"))
            return
        self.current = track
        self.status = "playing"
        self._play_id = await self.db.record_play(track["id"], self.output_getter())
        await self.backend.play(track["cache_path"], track.get("duration"))
        self.publish_state()

    async def skip(self) -> None:
        await self.backend.stop()
        await self._advance(completed=False)

    async def toggle_pause(self) -> None:
        if self.status == "playing":
            await self.backend.pause()
            self.status = "paused"
        elif self.status == "paused":
            await self.backend.resume()
            self.status = "playing"
        self.publish_state()

    async def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, volume))
        await self.backend.set_volume(self.volume)
        self.publish_state()

    async def enqueue_track_id(self, track_id: int, play_if_idle: bool = True) -> None:
        track = await self.db.track_by_id(track_id)
        if not track or track["cache_status"] != "ready":
            return
        if self.status == "idle" and play_if_idle:
            await self.play_track(track)
        else:
            self.queue.append(track)
            self.publish_state()

    async def play_day(self, date: str) -> None:
        """Archive mode: replay a whole day's tracks in posted order."""
        tracks = [
            t for t in await self.db.tracks_for_day(date) if t["cache_status"] == "ready"
        ]
        if not tracks:
            return
        await self.backend.stop()
        self.mode = "archive"
        self.queue = tracks[1:]
        await self.play_track(tracks[0])

    async def set_mode(self, mode: str) -> None:
        if mode in ("live", "archive"):
            self.mode = mode
            self.publish_state()

    # -- track end handling ---------------------------------------------------

    def _schedule_track_ended(self) -> None:
        asyncio.get_running_loop().create_task(self._advance(completed=True))

    async def _advance(self, completed: bool) -> None:
        if self._play_id is not None and completed:
            await self.db.mark_play_completed(self._play_id)
        self._play_id = None
        self.current = None
        if self.queue:
            await self.play_track(self.queue.pop(0))
        else:
            self.status = "idle"
            if self.mode == "archive":
                self.mode = "live"  # archive replay finished; fall back to live
            self.publish_state()

    # -- state ------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        def brief(t: dict[str, Any] | None) -> dict[str, Any] | None:
            if not t:
                return None
            return {
                "id": t["id"],
                "video_id": t["video_id"],
                "title": t.get("title"),
                "artist": t.get("artist"),
                "sender": t.get("sender"),
                "duration": t.get("duration"),
            }

        return {
            "status": self.status,
            "mode": self.mode,
            "volume": self.volume,
            "output": self.output_getter(),
            "current": brief(self.current),
            "queue": [brief(t) for t in self.queue],
        }

    def publish_state(self) -> None:
        self.bus.publish(PLAYER_STATE, self.state())
