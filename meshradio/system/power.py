"""Battery fuel gauge polling and safe shutdown (architecture §2 Power).

The UPS HAT exposes an I2C fuel gauge; ``UpsPowerMonitor`` will read it via
smbus2 once the HAT model is pinned down (Waveshare UPS HAT (B) / Geekworm
X728 use different registers — keep both behind this one class).

Dev/wall-powered builds get ``StaticPowerMonitor`` which reports mains power.
"""

from __future__ import annotations

import asyncio
import logging

from ..bus import EventBus, POWER_STATE
from ..runtime import Service

log = logging.getLogger(__name__)

SHUTDOWN_PERCENT = 5


class StaticPowerMonitor(Service):
    """No fuel gauge: report 100% on mains, forever."""

    def __init__(self, bus: EventBus):
        self.bus = bus

    async def _run(self) -> None:
        while True:
            self.bus.publish(POWER_STATE, {"percent": 100, "charging": True, "battery": False})
            await asyncio.sleep(60)


class UpsPowerMonitor(StaticPowerMonitor):
    """I2C fuel gauge polling + safe shutdown at SHUTDOWN_PERCENT.

    TODO(hw): implement register reads for the chosen UPS HAT; trigger
    ``systemctl poweroff`` via subprocess when percent < SHUTDOWN_PERCENT
    and not charging.
    """

    async def _run(self) -> None:  # pragma: no cover - hardware only
        log.warning("UpsPowerMonitor not implemented; reporting static power")
        await super()._run()
