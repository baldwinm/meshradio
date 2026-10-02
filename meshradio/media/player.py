"""Player service: queue + live-mode policy on top of a swappable backend.

Live mode policy (architecture §7, locked): a new track never interrupts the
current one. Idle in Live mode → auto-play; busy → enqueue; quiet hours
suppress auto-play. mpv does the actual decoding; a NullBackend keeps the
whole service testable and runnable on machines without libmpv.
"""

from __future__ import annotations

import logging
import math
import random
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..bus import PLAYER_STATE, TRACK_READY, EventBus
from ..config import PlayerConfig
from ..db import Database
from ..runtime import Service, spawn

# The engines live in backends.py; re-exported here because every caller and
# test imports them alongside PlayerService.
from .backends import Backend as Backend
from .backends import EmbedBackend as EmbedBackend
from .backends import MpvBackend as MpvBackend
from .backends import NullBackend as NullBackend
from .backends import WebBackend as WebBackend

log = logging.getLogger(__name__)


def _is_filler(track: dict[str, Any]) -> bool:
    """Is this queue entry station padding rather than a channel post?

    Radio tracks are filler by source (their rows exist only as Mix
    continuations). Archive-station entries are ordinary channel rows replayed
    as padding, so the row can't say — the queue entry carries a flag instead."""
    return track.get("source") == "radio" or bool(track.get("filler"))


def _duration(track: dict[str, Any] | None) -> float | None:
    """A usable length for the track, in seconds, or None.

    The archive refuses non-finite and absurd lengths at the row, so this is
    the belt to that brace: an old row or a stale snapshot must not be able
    to leave the clock unserialisable or a page unrenderable."""
    value = (track or {}).get("duration")
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None




class PlayerService(Service):
    def __init__(
        self,
        config: PlayerConfig,
        db: Database,
        bus: EventBus,
        backend: Backend,
        output_getter: Callable[[], str] = lambda: "speaker",
        events_out: EventBus | None = None,
    ):
        self.config = config
        self.db = db
        # Inbound events (TRACK_READY from the shared cacher) arrive on
        # ``bus``; state announcements go to ``events_out`` when given, so a
        # per-visitor session player (public embed hosting) broadcasts only
        # to its own browser's sockets instead of to every visitor.
        self.bus = bus
        self.events = events_out or bus
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
        # The station filling the queue when it runs dry, if any. "radio" pulls
        # a YouTube Mix from the seed track; "archive" replays random channel
        # history. Mutually exclusive: both answer "what plays after the last
        # queued song", so only one can be on.
        self.station: str | None = None    # None | "radio" | "archive"
        self.radio: Any = None             # RadioService, injected by app.py
        # The highest channel track id this player has accounted for — loaded
        # with a day, or announced live. Ids rise with ingestion, so anything on
        # the day above it is a song the player never saw; a restored session
        # (it had no live player while the channel went on posting) queues those.
        self.seen_track_id: int = 0
        self.on_state: Callable[[], None] | None = None  # session persistence hook
        self._play_id: int | None = None
        # Playback position clock: base seconds + wall time since epoch while
        # playing. With WebBackend the browser is the real transport, so this
        # is a close estimate kept in sync by /api/seek.
        self._pos_base: float = 0.0
        self._pos_epoch: float | None = None

    # -- lifecycle -----------------------------------------------------------

    async def stop(self) -> None:
        await super().stop()
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

    def _enqueue(self, track: dict[str, Any]) -> bool:
        """Add a track to the queue if it belongs there; says whether it did.

        Channel posts go ahead of station filler; filler appends at the end.
        A song that is already playing or already queued is not added again —
        a repost, a double press on "+ queue" — and the queue has a ceiling
        (``max_queue``): a visitor pressing "+ queue" a thousand times used to
        get a thousand entries, with every state push and session snapshot
        growing to match. At the ceiling a channel post still gets in by
        displacing the last piece of filler; more filler is simply not added."""
        video_id = track.get("video_id")
        if video_id and any(
            t.get("video_id") == video_id for t in (self.current, *self.queue) if t
        ):
            return False
        if len(self.queue) >= max(1, int(self.config.max_queue)):
            if _is_filler(track):
                return False
            last_filler = next(
                (i for i in range(len(self.queue) - 1, -1, -1) if _is_filler(self.queue[i])),
                None,
            )
            if last_filler is None:
                return False
            self.queue.pop(last_filler)
        if _is_filler(track):
            self.queue.append(track)
        else:
            idx = next(
                (i for i, t in enumerate(self.queue) if _is_filler(t)), len(self.queue)
            )
            self.queue.insert(idx, track)
        return True

    async def on_track_ready(self, track: dict[str, Any]) -> None:
        # Radio tracks were explicitly requested (even if radio has been
        # switched off since — stopping only halts NEW mix fetches); channel
        # tracks only enter the jukebox if freshly posted (older ones are
        # archive backfill).
        if track.get("source") != "radio" and not self._is_fresh(track):
            return
        if track.get("source") != "radio":
            self.seen_track_id = max(self.seen_track_id, track["id"])
        if (
            self.status == "idle"
            and self.mode == "live"
            and self.config.live_autoplay
            and not self.in_quiet_hours()
        ):
            await self.play_track(track)
        elif self._enqueue(track):
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

    def _is_playable(self, track: dict[str, Any]) -> bool:
        """Can this player start the track? The appliance needs a downloaded
        file (cache_status 'ready'). Embed hosting streams every video by id in
        the browser, so a track is playable the moment it's ingested — metadata
        (oEmbed) is just for display, and its fetch is easily throttled from a
        datacenter IP. Only rows marked 'failed' (deleted/unavailable) are
        dropped there, so a whole day's playlist shows up, not just the handful
        that happened to get metadata."""
        if self.embed:
            return track["cache_status"] != "failed"
        return track["cache_status"] == "ready"

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
        await self.backend.play(track["cache_path"], _duration(track))
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
        duration = _duration(self.current)
        if duration:
            pos = min(pos, duration)
        return max(pos, 0.0)

    async def seek(self, seconds: float) -> None:
        """Jump within the current track. The backend follows; for WebBackend
        the speaker tab either initiated this or follows via the state push."""
        if self.current is None or not math.isfinite(seconds):
            return
        duration = _duration(self.current)
        seconds = max(0.0, min(seconds, duration) if duration else seconds)
        self._pos_base = seconds
        self._pos_epoch = time.monotonic() if self.status == "playing" else None
        await self.backend.seek(seconds)
        self.publish_state()

    async def set_volume(self, volume: int) -> None:
        self.volume = max(0, min(100, volume))
        await self.backend.set_volume(self.volume)
        self.publish_state()

    async def enqueue_track_id(self, track_id: int, play_if_idle: bool = True) -> bool:
        """Queue (or, if idle, play) a track by id. False if nothing changed:
        no such playable track, or it's already playing or queued."""
        track = await self.db.track_by_id(track_id)
        if not track or not self._is_playable(track):
            return False
        if self.status == "idle" and play_if_idle:
            await self.play_track(track)
            return True
        if not self._enqueue(track):
            return False
        self.publish_state()
        return True

    async def _read_day(self, date: str) -> tuple[list[dict[str, Any]], int]:
        """The day's songs this player can start, in posted order, and the
        highest track id the day holds — playable or not, since a failed row
        is as read as any other. That id is how far a player that loads the
        day has read it (``seen_track_id``)."""
        rows = await self.db.tracks_for_day(date)
        return (
            [t for t in rows if self._is_playable(t)],
            max((t["id"] for t in rows), default=0),
        )

    async def play_day(self, date: str) -> None:
        """Archive mode: replay a whole day's tracks in posted order."""
        tracks, seen = await self._read_day(date)
        if not tracks:
            return
        await self.backend.stop()
        self.mode = "archive"
        self.day = date
        self.seen_track_id = seen
        self.queue = tracks[1:]
        await self.play_track(tracks[0])

    async def cue_day(self, date: str) -> bool:
        """Load a day like play_day but parked at 0:00 in 'paused' — a new
        visitor lands with music ready instead of an empty player. No play
        row is recorded until something actually plays."""
        tracks, seen = await self._read_day(date)
        if not tracks:
            return False
        self.mode = "archive"
        self.day = date
        self.seen_track_id = seen
        self.current = tracks[0]
        self.queue = tracks[1:]
        self.status = "paused"
        self._pos_base = 0.0
        self._pos_epoch = None
        self.publish_state()
        return True

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

    async def shuffle_queue(self) -> None:
        """Randomly reorder the upcoming queue. The current track keeps
        playing untouched; only what comes next is shuffled."""
        if len(self.queue) > 1:
            random.shuffle(self.queue)
            self.publish_state()

    async def clear_queue(self) -> None:
        """Empty the queue. Also switches the station off — otherwise it would
        immediately refill what the user just cleared."""
        self.queue = []
        self.station = None
        self.publish_state()

    # -- stations (queue filler when the day runs out) --------------------------

    async def start_station(self, kind: str, track_id: int | None = None) -> bool:
        """Turn on a station, so the music keeps going once the queue is empty.

        ``radio`` seeds a YouTube Mix off a track (default: whatever is playing,
        else the last thing played) — it needs yt-dlp and a residential IP.
        ``archive`` replays random channel history and needs neither, which is
        what makes it the one that works on the public embed host.

        Starting from idle plays immediately: a visitor who reaches the end of a
        day and presses this wants music now, not on the next advance."""
        if kind == "radio":
            return await self._start_radio(track_id)
        if kind != "archive":
            log.warning("unknown station %r", kind)
            return False
        self.station = "archive"
        await self._extend_from_archive()
        if self.status == "idle" and self.queue:
            await self.play_track(self.queue.pop(0))
        else:
            self.publish_state()
        return bool(self.queue or self.current)

    async def _start_radio(self, track_id: int | None = None) -> bool:
        if self.radio is None:
            return False
        seed = None
        if track_id is not None:
            seed = await self.db.track_by_id(track_id)
        elif self.current is not None:
            seed = await self.db.track_by_id(self.current["id"])
        else:
            seed = await self.db.last_played_track()
        if seed is None:
            return False
        self.station = "radio"
        self.publish_state()
        await self.radio.extend(seed, limit=self.config.radio_batch)
        return True

    async def stop_station(self) -> None:
        """Stop topping the queue up. Whatever the station already queued (or is
        still downloading) stays — clear_queue is the way to drop it."""
        self.station = None
        self.publish_state()

    async def _extend_from_archive(self) -> int:
        """Append a batch of random archived songs to the queue as filler.

        Synchronous and offline: the rows are already in the archive, so unlike
        radio mode there's no fetch, no cacher round trip, and nothing to wait
        for — the next song is ready the instant the last one ends."""
        seen = [t["video_id"] for t in (self.current, *self.queue) if t]
        tracks = await self.db.random_channel_tracks(
            limit=self.config.station_batch,
            exclude_video_ids=seen,
            ready_only=not self.embed,
        )
        added = sum(self._enqueue(dict(track, filler=True)) for track in tracks)
        if not tracks:
            log.info("archive station: nothing playable to queue")
        return added

    def _maybe_extend_radio(self, seed: dict[str, Any] | None) -> None:
        if self.station == "radio" and self.radio is not None and seed is not None:
            spawn("radio-extend", self.radio.extend(seed, limit=self.config.radio_batch))

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
        (oEmbed metadata has no duration, so embed tracks start without one).

        Fills a blank only. The report comes from whichever browser is
        playing, and the row is shared by every session, so a value that is
        already known is never overwritten — the client sends one exactly
        when the track has none, and the server holds it to the same rule."""
        if not (seconds > 0 and math.isfinite(seconds)):
            return
        await self.db.fill_track_duration(track_id, seconds)
        changed = False
        for t in [self.current, *self.queue]:
            if t and t["id"] == track_id and not t.get("duration"):
                t["duration"] = seconds
                changed = True
        if changed:
            self.publish_state()

    # -- track end handling ---------------------------------------------------

    def _schedule_track_ended(self) -> None:
        spawn("player-advance", self._advance(completed=True))

    async def _advance(self, completed: bool) -> None:
        if self._play_id is not None and completed:
            await self.db.mark_play_completed(self._play_id)
        self._play_id = None
        last = self.current
        self.current = None
        # The archive station refills right here, with no network round trip, so
        # the last song of a day rolls straight into the next pick instead of
        # dropping the listener into silence.
        if not self.queue and self.station == "archive":
            await self._extend_from_archive()
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
                "duration": _duration(t),
                "source": t.get("source"),
                "filler": bool(t.get("filler")),
            }

        return {
            "status": self.status,
            "mode": self.mode,
            "day": self.day,
            "position": round(self.position(), 1),
            "volume": self.volume,
            "output": self.output_getter(),
            "station": self.station,
            "web_audio": isinstance(self.backend, WebBackend),
            "embed": self.embed,
            "current": brief(self.current),
            "queue": [brief(t) for t in self.queue],
        }

    def publish_state(self) -> None:
        self.events.publish(PLAYER_STATE, self.state())
        if self.on_state is not None:
            self.on_state()

    # -- session persistence (embed hosting: survive deploys) -------------------

    def snapshot(self) -> dict[str, Any]:
        """Persistable state for a visitor session."""
        return {
            "day": self.day,
            "mode": self.mode,
            "status": self.status,
            "volume": self.volume,
            "station": self.station,
            "current_track_id": self.current["id"] if self.current else None,
            "seen_track_id": self.seen_track_id,
            "position": round(self.position(), 1),
            "saved_at": time.time(),
            "queue_track_ids": [t["id"] for t in self.queue],
        }

    async def restore(self, snap: dict[str, Any]) -> None:
        """Rebuild from a snapshot. Tracks that vanished or lost readiness
        since the save are skipped; a playing position advances by the wall
        time since the save, clamped inside the track."""
        self.volume = int(snap.get("volume", self.volume))
        self.mode = "archive" if snap.get("mode") == "archive" else "live"
        day = snap.get("day")
        self.day = day if isinstance(day, str) else None
        # A station survives the restart that dropped the session; the queue's
        # per-entry filler flags don't (only track ids are stored), which at
        # worst costs a restored session one mis-ordered live post.
        self.station = snap.get("station") if snap.get("station") in ("radio", "archive") else None
        queue_ids = list(snap.get("queue_track_ids", []))
        current_id = snap.get("current_track_id")
        # One query for the lot: a restore is a returning visitor's first
        # request, and a long day is a couple of hundred ids.
        wanted = queue_ids + ([current_id] if current_id is not None else [])
        rows = await self.db.tracks_by_ids(wanted)
        current = rows.get(current_id) if current_id is not None else None
        if current and not self._is_playable(current):
            current = None
        status = snap.get("status")
        if current and status in ("playing", "paused"):
            self.current = current
            self.status = status
        # The queue is rebuilt through _enqueue, so a snapshot from before the
        # ceiling (or a tampered one) comes back deduplicated and bounded.
        self.queue = []
        for i in queue_ids:
            if i in rows and self._is_playable(rows[i]):
                self._enqueue(dict(rows[i]))
        # Nothing was listening for new songs between the save and now (the
        # idle session was reaped, or the process restarted), so the day may
        # hold posts this player never saw. Queue them behind what it has.
        # A snapshot saved before the mark existed is caught up from the
        # furthest song it still holds, which keeps a deploy from re-queueing
        # a whole day the visitor has already played through.
        seen = snap.get("seen_track_id")
        if isinstance(seen, bool) or not isinstance(seen, int):
            seen = max(wanted, default=0)
        self.seen_track_id = seen
        if self.day is not None:
            tracks, high = await self._read_day(self.day)
            for track in tracks:
                if track["id"] > seen:
                    self._enqueue(dict(track))
            self.seen_track_id = max(seen, high)
        if current and status in ("playing", "paused"):
            position = max(float(snap.get("position") or 0.0), 0.0)
            if status == "playing" and snap.get("saved_at"):
                position += max(0.0, time.time() - float(snap["saved_at"]))
            duration = _duration(current)
            if duration:
                position = min(position, max(duration - 1.0, 0.0))
            self._pos_base = position
            self._pos_epoch = time.monotonic() if status == "playing" else None
        self.publish_state()
