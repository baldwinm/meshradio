"""Front panel: OLED (luma.oled) + rotary encoder and buttons (gpiozero).

The panel is a bus subscriber like everything else: it renders
``player.state`` / ``power.state`` / ``output.changed`` and translates
physical inputs into PlayerService calls.

Hardware imports are guarded; on dev machines ``LogPanel`` just logs state
changes so the rest of the app behaves identically. The five screens
(NowPlaying, Queue, Archive, Outputs, Status — architecture §9) will grow in
``ui/screens/`` once rendering is validated on the real SSD1309.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..bus import EventBus, OUTPUT_CHANGED, PLAYER_STATE, POWER_STATE

log = logging.getLogger(__name__)


class LogPanel:
    """Dev stand-in: logs what the OLED would show."""

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="panel")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _run(self) -> None:
        sub = self.bus.subscribe(PLAYER_STATE, POWER_STATE, OUTPUT_CHANGED)
        try:
            async for topic, payload in sub:
                if topic == PLAYER_STATE:
                    self._render_now_playing(payload)
                else:
                    log.info("[panel] %s: %s", topic, payload)
        finally:
            sub.close()

    def _render_now_playing(self, state: dict[str, Any]) -> None:
        current = state.get("current")
        if current:
            # ASCII only: dev consoles (Windows cp1252) choke on music glyphs
            log.info(
                "[panel] now playing: %s - %s (from %s) [%s, vol %s]",
                current.get("artist") or "?",
                current.get("title") or current.get("video_id"),
                current.get("sender") or "?",
                state["status"],
                state["volume"],
            )
        else:
            log.info("[panel] idle - queue %d [%s]", len(state.get("queue", [])), state["mode"])


class OledPanel(LogPanel):
    """Real hardware panel. Constructed only on pi4/lite profiles."""

    def __init__(self, bus: EventBus, player, router):
        super().__init__(bus)
        self.player = player
        self.router = router
        # Deferred imports: these only exist on the appliance image.
        from gpiozero import Button, RotaryEncoder  # type: ignore
        from luma.core.interface.serial import i2c  # type: ignore
        from luma.oled.device import ssd1309  # type: ignore

        self.device = ssd1309(i2c(port=1, address=0x3C))
        self.encoder = RotaryEncoder(a=17, b=27, max_steps=0)
        self.encoder_btn = Button(22)
        self.next_btn = Button(23)
        self.mode_btn = Button(24)
        self._wire_inputs()

    def _wire_inputs(self) -> None:
        loop = asyncio.get_event_loop()

        def call(coro) -> None:
            asyncio.run_coroutine_threadsafe(coro, loop)

        self.encoder.when_rotated_clockwise = lambda: call(
            self.player.set_volume(self.player.volume + 5)
        )
        self.encoder.when_rotated_counter_clockwise = lambda: call(
            self.player.set_volume(self.player.volume - 5)
        )
        self.encoder_btn.when_pressed = lambda: call(self.player.toggle_pause())
        self.next_btn.when_pressed = lambda: call(self.player.skip())
        self.mode_btn.when_pressed = lambda: call(
            self.player.set_mode("archive" if self.player.mode == "live" else "live")
        )

    def _render_now_playing(self, state: dict[str, Any]) -> None:
        # TODO(v0.2): real luma.oled canvas rendering + marquee; validate on hw.
        from luma.core.render import canvas  # type: ignore

        current = state.get("current") or {}
        with canvas(self.device) as draw:
            draw.text((0, 0), (current.get("title") or "MeshRadio")[:21], fill="white")
            draw.text((0, 16), (current.get("artist") or "")[:21], fill="white")
            draw.text((0, 32), f"from {current.get('sender') or '—'}"[:21], fill="white")
            draw.text((0, 48), f"{state['status']}  vol {state['volume']}", fill="white")


def make_panel(profile: str, bus: EventBus, player, router):
    if profile in ("pi4", "lite"):
        try:
            return OledPanel(bus, player, router)
        except Exception:
            log.exception("OLED init failed; falling back to log panel")
    return LogPanel(bus)
