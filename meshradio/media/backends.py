"""Playback engines behind ``PlayerService``: a protocol and four engines.

``NullBackend`` simulates playback for tests and ``--demo``; ``WebBackend``
and ``EmbedBackend`` are state-only, with the browser as the output device
(streaming the cache or YouTube's player respectively); ``MpvBackend`` drives
mpv on the appliance.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol


class Backend(Protocol):
    """What the player needs from a playback engine. ``on_end`` is set by the
    service and called by the engine when a track finishes."""

    on_end: Callable[[], None] | None

    async def play(self, path: str, duration: float | None) -> None: ...
    async def stop(self) -> None: ...
    async def pause(self) -> None: ...
    async def resume(self) -> None: ...
    async def set_volume(self, volume: int) -> None: ...
    async def seek(self, seconds: float) -> None: ...


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
