"""Audio output routing. PipeWire owns routing; we just pick the default sink.

Three backends behind one ``set_output()`` interface (architecture §8, §13):

- ``WpctlRouter`` (pi4): speaker / jack / BT are three PipeWire sinks; switch
  the default sink with ``wpctl``.
- ``LiteRouter`` (lite): speaker + jack share one I2S sink; "switching" toggles
  the MAX98357A shutdown GPIO. BT stays a separate PipeWire sink.
- ``DevRouter`` (dev): in-memory no-op.

Nothing above this layer knows which build it's on.
"""

from __future__ import annotations

import asyncio
import logging
import re

from ..bus import EventBus, OUTPUT_CHANGED

log = logging.getLogger(__name__)

OUTPUTS = ("speaker", "jack", "bluetooth")


class DevRouter:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._current = "speaker"

    def outputs(self) -> list[str]:
        return list(OUTPUTS)

    def current(self) -> str:
        return self._current

    async def set_output(self, name: str) -> bool:
        if name not in OUTPUTS:
            return False
        self._current = name
        self.bus.publish(OUTPUT_CHANGED, {"output": name})
        return True


class WpctlRouter:
    """Full-kit backend: map logical outputs to PipeWire sinks via wpctl.

    Sink match patterns are configurable-in-code for now; they match the
    Bookworm defaults for the I2S amp (MAX98357A), the Pi 4 headphone jack,
    and any bluez A2DP sink.
    """

    SINK_PATTERNS = {
        "speaker": re.compile(r"max98357|hifiberry|i2s", re.I),
        "jack": re.compile(r"headphones|bcm2835", re.I),
        "bluetooth": re.compile(r"bluez", re.I),
    }

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._current = "speaker"

    def outputs(self) -> list[str]:
        return list(OUTPUTS)

    def current(self) -> str:
        return self._current

    async def set_output(self, name: str) -> bool:
        pattern = self.SINK_PATTERNS.get(name)
        if not pattern:
            return False
        sink_id = await self._find_sink(pattern)
        if sink_id is None:
            log.warning("no PipeWire sink found for output %r", name)
            return False
        proc = await asyncio.create_subprocess_exec("wpctl", "set-default", sink_id)
        await proc.wait()
        if proc.returncode != 0:
            return False
        self._current = name
        self.bus.publish(OUTPUT_CHANGED, {"output": name})
        return True

    async def _find_sink(self, pattern: re.Pattern) -> str | None:
        proc = await asyncio.create_subprocess_exec(
            "wpctl", "status", stdout=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        in_sinks = False
        for line in stdout.decode(errors="replace").splitlines():
            if "Sinks:" in line:
                in_sinks = True
                continue
            if in_sinks:
                if not line.strip() or "Sources:" in line:
                    break
                match = re.search(r"(\d+)\.", line)
                if match and pattern.search(line):
                    return match.group(1)
        return None


class LiteRouter(WpctlRouter):
    """Lite backend: speaker and jack share the I2S sink; the MAX98357A SD
    (shutdown) pin mutes the speaker when Line Out is selected."""

    AMP_ENABLE_GPIO = 16  # BCM pin driving the MAX98357A SD pad

    SINK_PATTERNS = {
        "speaker": re.compile(r"max98357|pcm5102|i2s|sndrpihifiberry", re.I),
        "jack": re.compile(r"max98357|pcm5102|i2s|sndrpihifiberry", re.I),
        "bluetooth": re.compile(r"bluez", re.I),
    }

    def __init__(self, bus: EventBus):
        super().__init__(bus)
        try:
            from gpiozero import DigitalOutputDevice  # type: ignore

            self._amp = DigitalOutputDevice(self.AMP_ENABLE_GPIO, initial_value=True)
        except Exception:
            log.warning("gpiozero unavailable; amp-enable GPIO disabled")
            self._amp = None

    async def set_output(self, name: str) -> bool:
        ok = await super().set_output(name)
        if ok and self._amp is not None:
            if name == "speaker":
                self._amp.on()
            else:
                self._amp.off()
        return ok


def make_router(profile: str, bus: EventBus):
    if profile == "pi4":
        return WpctlRouter(bus)
    if profile == "lite":
        return LiteRouter(bus)
    return DevRouter(bus)
