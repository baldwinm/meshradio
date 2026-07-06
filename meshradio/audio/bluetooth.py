"""Bluetooth pairing state machine over BlueZ D-Bus (architecture §8).

SKELETON — the pairing flow (scan → select → pair → PipeWire picks up the
A2DP sink → auto-route) will be built against BlueZ's D-Bus API via
``dbus-fast`` once we're testing on real hardware. The interface below is
what the OLED menu and web UI program against, so it lands now.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)


class BluetoothManager:
    """States: idle → scanning → pairing → connected."""

    def __init__(self) -> None:
        self.state = "idle"
        self.devices: list[dict[str, Any]] = []

    @property
    def available(self) -> bool:
        return False  # flips to a real check when the BlueZ backend lands

    async def scan(self) -> list[dict[str, Any]]:
        raise NotImplementedError("BlueZ backend not implemented yet")

    async def pair(self, address: str) -> bool:
        raise NotImplementedError("BlueZ backend not implemented yet")

    async def disconnect(self) -> None:
        raise NotImplementedError("BlueZ backend not implemented yet")
