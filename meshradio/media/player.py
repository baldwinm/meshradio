"""Player service: queue + live-mode policy on top of a swappable backend.

Live mode policy (architecture §7, locked): a new track never interrupts the
current one. Idle in Live mode → auto-play; busy → enqueue; quiet hours
suppress auto-play. mpv does the actual decoding; a NullBackend keeps the
whole service testable and runnable on machines without libmpv.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
        self._duration = 3.0

    async def play(self, path: str, duration: float | None) -> None:
        await self.stop()
        self.paused = False
        self._duration = duration or 3.0
        self._timer = asyncio.create_task(self._finish_after(self._duration * self.time_scale))

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

    async def seek(self, seconds: float) -> None:
        if self._timer:
            self._timer.cancel()
            remaining = max(self._duration - seconds, 0.0) * self.time_scale
            self._timer = asyncio.create_task(self._finish_after(remaining))


class WebBackend:
    """Playback happens in connected browsers: the page's <audio> element
    streams /audio/{id} and reports track end via POST /api/ended/{id}.
    The server keeps authoritative state; this backend is state-only."""

    def __init__(self) -> None:
        self.on_end: Callable[[], None] | None = None
        self.volume = 100
        self.paused = False

    async def play(self, path: str, duration: float | None) -> None:
        self.paused = False

    async def stop(self) -> None:
        pass

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def set_volume(self, volume: int) -> None:
        self.volume = volume

    async def seek(self, seconds: float) -> None:
        pass  # the speaker tab moves its own <audio> element


class EmbedBackend:
    """Playback happens in connected browsers via the YouTube IFrame player:
    the speaker tab streams straight from YouTube, so no audio ever touches
    this server — the mode for public hosting. Tracks are queued by video id
    with no cached file; metadata comes from oEmbed and the embed player
    reports real durations back via /api/duration. State-only, like
    WebBackend."""

    def __init__(self) -> None:
        self.on_end: Callable[[], None] | None = None
        self.volume = 100
        self.paused = False

    async def play(self, path: str | None, duration: float | None) -> None:
        self.paused = False

    async def stop(self) -> None:
        pass

    async def pause(self) -> None:
        self.paused = True

    async def resume(self) -> None:
        self.paused = False

    async def set_volume(self, volume: int) -> None:
        self.volume = volume

    async def seek(self, seconds: float) -> None:
        pass  # the speaker tab seeks its own iframe player


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

    async def seek(self, seconds: float) -> None:
        self._player.seek(seconds, reference="absolute")


class PlayerService:
    def __init__(
        self,
        config: PlayerConfig,
        db: Database,
        bus: EventBus,
        backend: NullBackend | WebBackend | MpvBackend,
        output_getter: Callable[[], str] = lambda: "speaker",
    ):
        self.config = config
        self.db = db
        self.bus = bus
        self.backend = backend
        self.embed = isinstance(backend, EmbedBackend)
        self.backend.on_end = self._schedule_track_ended
        self.output_getter = output_getter
        self.tz = ZoneInfo(config.timezone)

        self.status: str = "idle"          # idle | playing | paused
        self.mode: str = "live"            # live | archive
        self.day: str | None = None        # archive date being replayed (None = live/today)
        self.current: dict[str, Any] | None = None
        self.queue: list[dict[str, Any]] = []
        self.volume: int = config.volume
        self.radio_active: bool = False
        self.radio: Any = None             # RadioService, injected by app.py
        self._play_id: int | None = None
        self._task: asyncio.Task | None = None
        # Playback position clock: base seconds + wall time since epoch while
        # playing. With WebBackend the browser is the real transport, so this
        # is a close estimate kept in sync by /api/seek.
        self._pos_base: float = 0.0
        self._pos_epoch: float | None = None

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

    def _is_fresh(self, track: dict[str, Any]) -> bool:
        """Was this posted recently? Cache completions of backfilled history
        (first boot, retry sweeps) must not hijack the jukebox."""
        mesh_ts = track.get("mesh_ts")
        if mesh_ts is None:
            return True
        return (time.time() - float(mesh_ts)) <= self.config.live_window_s

    def _enqueue(self, track: dict[str, Any]) -> None:
        """Channel tracks go ahead of radio filler; radio appends at the end."""
        if track.get("source") == "radio":
            self.queue.append(track)
        else:
            idx = next(
                (i for i, t in enumerate(self.queue) if t.get("source") == "radio"),
                len(self.queue),
            )
            self.queue.insert(idx, track)

    async def on_track_ready(self, track: dict[str, Any]) -> None:
        # Radio tracks were explicitly requested (even if radio has been
        # switched off since — stopping only halts NEW mix fetches); channel
        # tracks only enter the jukebox if freshly posted (older ones are
        # archive backfill).
        if track.get("source") != "radio" and not self._is_fresh(track):
            return
        if (
            self.status == "idle"
            and self.mode == "live"
            and self.config.live_autoplay
            and not self.in_quiet_hours()
        ):
            await self.play_track(track)
        else:
            self._enqueue(track)
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
        # Embed mode streams by video id in the browser; no local file needed.
        if not track.get("cache_path") and not self.embed:
            log.warning("track %s has no cached audio; skipping", track.get("video_id"))
            return
        self.current = track
        self.status = "playing"
        self._pos_base = 0.0
        self._pos_epoch = time.monotonic()
        self._play_id = await self.db.record_play(track["id"], self.output_getter())
        await self.backend.play(track["cache_path"], track.get("duration"))
        self.publish_state()

    async def skip(self) -> None:
        await self.backend.stop()
        await self._advance(completed=False)

    async def toggle_pause(self) -> None:
        if self.status == "playing":
            self._pos_base = self.position()
            self._pos_epoch = None
            await self.backend.pause()
            self.status = "paused"
        elif self.status == "paused":
            self._pos_epoch = time.monotonic()
            await self.backend.resume()
            self.status = "playing"
        self.publish_state()

    def position(self) -> float:
        """Seconds into the current track (clamped to its duration)."""
        pos = self._pos_base
        if self._pos_epoch is not None:
            pos += time.monotonic() - self._pos_epoch
        duration = (self.current or {}).get("duration")
        if duration:
            pos = min(pos, float(duration))
        return max(pos, 0.0)

    async def seek(self, seconds: float) -> None:
        """Jump within the current track. The backend follows; for WebBackend
        the speaker tab either initiated this or follows via the state push."""
        if self.current is None:
            return
        duration = self.current.get("duration")
        seconds = max(0.0, min(seconds, float(duration)) if duration else seconds)
        self._pos_base = seconds
        self._pos_epoch = time.monotonic() if self.status == "playing" else None
        await self.backend.seek(seconds)
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
            self._enqueue(track)
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
        self.day = date
        self.queue = tracks[1:]
        await self.play_track(tracks[0])

    async def set_mode(self, mode: str) -> None:
        if mode in ("live", "archive"):
            self.mode = mode
            if mode == "live":
                self.day = None
            self.publish_state()

    # -- queue editing -----------------------------------------------------

    def _queue_entry(self, index: int, track_id: int) -> dict[str, Any] | None:
        """The queue item at ``index`` iff it's still the track the client
        was looking at — the queue may have shifted since their page render."""
        if 0 <= index < len(self.queue) and self.queue[index]["id"] == track_id:
            return self.queue[index]
        return None

    async def remove_from_queue(self, index: int, track_id: int) -> bool:
        if self._queue_entry(index, track_id) is None:
            return False
        self.queue.pop(index)
        self.publish_state()
        return True

    async def move_to_front(self, index: int, track_id: int) -> bool:
        if self._queue_entry(index, track_id) is None:
            return False
        self.queue.insert(0, self.queue.pop(index))
        self.publish_state()
        return True

    async def clear_queue(self) -> None:
        """Empty the queue. Also switches radio off — otherwise it would
        immediately refill what the user just cleared."""
        self.queue = []
        self.radio_active = False
        self.publish_state()

    # -- radio mode -----------------------------------------------------------

    async def start_radio(self, track_id: int | None = None) -> bool:
        """Start a YouTube Mix 'radio station' seeded from a track (default:
        whatever is playing, else the most recently played track)."""
        if self.radio is None:
            return False
        seed = None
        if track_id is not None:
            seed = await self.db.track_by_id(track_id)
        elif self.current is not None:
            seed = await self.db.track_by_id(self.current["id"])
        else:
            rows = await self.db._fetchall(
                "SELECT tr.* FROM tracks tr JOIN plays p ON p.track_id=tr.id "
                "ORDER BY p.played_at DESC LIMIT 1"
            )
            seed = rows[0] if rows else None
        if seed is None:
            return False
        self.radio_active = True
        self.publish_state()
        await self.radio.extend(seed, limit=self.config.radio_batch)
        return True

    async def stop_radio(self) -> None:
        """Stop fetching new mix batches. Radio tracks already queued (or
        still downloading) are kept — clear_queue is the way to drop them."""
        self.radio_active = False
        self.publish_state()

    def _maybe_extend_radio(self, seed: dict[str, Any] | None) -> None:
        if self.radio_active and self.radio is not None and seed is not None:
            asyncio.get_running_loop().create_task(
                self.radio.extend(seed, limit=self.config.radio_batch)
            )

    # -- browser playback signal ------------------------------------------------

    async def notify_ended(self, track_id: int) -> bool:
        """A browser finished playing a track (WebBackend). Guarded by track
        id so duplicate signals from multiple tabs advance only once."""
        if self.status != "playing" or not self.current or self.current["id"] != track_id:
            return False
        await self._advance(completed=True)
        return True

    async def report_duration(self, track_id: int, seconds: float) -> None:
        """The embed speaker tab learned the real duration from its player
        (oEmbed metadata has no duration, so embed tracks start without one)."""
        if seconds <= 0:
            return
        await self.db.update_track_metadata(track_id, duration=seconds)
        changed = False
        for t in [self.current, *self.queue]:
            if t and t["id"] == track_id and not t.get("duration"):
                t["duration"] = seconds
                changed = True
        if changed:
            self.publish_state()

    # -- track end handling ---------------------------------------------------

    def _schedule_track_ended(self) -> None:
        asyncio.get_running_loop().create_task(self._advance(completed=True))

    async def _advance(self, completed: bool) -> None:
        if self._play_id is not None and completed:
            await self.db.mark_play_completed(self._play_id)
        self._play_id = None
        last = self.current
        self.current = None
        if self.queue:
            next_track = self.queue.pop(0)
            # Keep the radio rolling: top up when the queue is nearly dry.
            if len(self.queue) == 0:
                self._maybe_extend_radio(next_track)
            await self.play_track(next_track)
        else:
            self.status = "idle"
            self._pos_base = 0.0
            self._pos_epoch = None
            if self.mode == "archive":
                self.mode = "live"  # archive replay finished; fall back to live
                self.day = None
            self._maybe_extend_radio(last)
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
                "source": t.get("source"),
            }

        return {
            "status": self.status,
            "mode": self.mode,
            "day": self.day,
            "position": round(self.position(), 1),
            "volume": self.volume,
            "output": self.output_getter(),
            "radio": self.radio_active,
            "web_audio": isinstance(self.backend, WebBackend),
            "embed": self.embed,
            "current": brief(self.current),
            "queue": [brief(t) for t in self.queue],
        }

    def publish_state(self) -> None:
        self.bus.publish(PLAYER_STATE, self.state())
