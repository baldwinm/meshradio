"""Mesh ingestion — MeshCore companion node on USB serial (primary path).

Requires the ``meshcore`` library and a Heltec V3 running companion radio
firmware. Import is guarded so dev machines and Lite builds (no node) run
without it; enable via ``[mesh] enabled = true`` in config.
"""

from __future__ import annotations

import asyncio
import logging
import time

from ..bus import EventBus, INGEST_STATUS
from ..config import MeshConfig
from ..runtime import Service
from .service import IngestService

log = logging.getLogger(__name__)

try:
    import meshcore  # type: ignore

    HAVE_MESHCORE = True
except ImportError:
    HAVE_MESHCORE = False


class MeshIngest(Service):
    def __init__(self, config: MeshConfig, service: IngestService, bus: EventBus):
        self.config = config
        self.service = service
        self.bus = bus

    def start(self) -> None:
        if not HAVE_MESHCORE:
            log.warning("meshcore library not installed; mesh ingestion disabled")
            self.bus.publish(INGEST_STATUS, {"mesh": "unavailable"})
            return
        super().start()

    async def _run(self) -> None:
        """Connect to the companion node and stream channel messages.

        Reconnects with backoff on serial errors — RF nodes get unplugged.
        NOTE: written against the published `meshcore` (meshcore-py) API;
        validate on real hardware before the v0.1 image build.
        """
        backoff = 5
        while True:
            try:
                mc = await meshcore.MeshCore.create_serial(  # type: ignore[attr-defined]
                    self.config.serial_port or None
                )
                self.bus.publish(INGEST_STATUS, {"mesh": "connected"})
                log.info("mesh node connected on %s", self.config.serial_port or "auto")
                backoff = 5
                async for event in mc.subscribe(meshcore.EventType.CHANNEL_MSG_RECV):  # type: ignore[attr-defined]
                    await self._on_message(event.payload)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("mesh connection lost; retrying in %ss", backoff)
                self.bus.publish(INGEST_STATUS, {"mesh": "disconnected"})
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 120)

    async def _on_message(self, payload: dict) -> None:
        text = payload.get("text", "")
        sender = payload.get("sender") or payload.get("pubkey_prefix") or "unknown"
        ts = float(payload.get("sender_timestamp") or time.time())
        await self.service.handle_message(
            sender=str(sender), text=text, ts=ts, source="mesh"
        )
