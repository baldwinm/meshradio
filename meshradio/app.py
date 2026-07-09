"""MeshRadio entrypoint — one asyncio process, modules wired over the bus.

Startup order: DB → bus → ingest (mesh + CoreScope) → cacher → player →
routing → panel → power → web. Everything is a task in one loop; systemd
manages the process (architecture §4).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

import uvicorn

from . import __version__
from .audio.routing import make_router
from . import backup as backup_mod
from .backup import BackupService
from .bus import EventBus
from .config import load_config
from .db import Database
from .ingest.corescope import CoreScopePoller
from .ingest.mesh import MeshIngest
from .ingest.relay import RelayPusher
from .ingest.service import IngestService
from .media.cacher import Cacher
from .media.player import EmbedBackend, MpvBackend, NullBackend, PlayerService, WebBackend
from .media.radio import RadioService
from .runtime import spawn
from .system.power import StaticPowerMonitor, UpsPowerMonitor
from .ui.panel import make_panel
from .web.server import create_app

log = logging.getLogger("meshradio")


def make_backend(profile: str, choice: str = "auto"):
    """Pick the playback engine. "auto": mpv on appliance profiles (audio out
    the Pi's sinks), web playback everywhere else (the browser is the
    speaker until hardware exists). "embed" streams via the YouTube IFrame
    player in the browser — the no-download mode for public hosting."""
    if choice == "auto":
        choice = "mpv" if profile in ("pi4", "lite") else "web"
    if choice == "embed":
        return EmbedBackend()
    if choice == "mpv":
        try:
            return MpvBackend()
        except Exception:
            log.exception("mpv unavailable; falling back to web playback")
            choice = "web"
    if choice == "web":
        return WebBackend()
    return NullBackend()


async def seed_demo(config, ingest: IngestService) -> None:
    """Dev-only: push a fake channel day through the real pipeline. Cache files
    are pre-touched so the cacher marks them ready and the (Null) player runs
    the whole live-mode flow without yt-dlp/ffmpeg on the machine."""
    import time

    demo_videos = ["dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk"]
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    for vid in demo_videos:
        (config.cache_dir / f"{vid}.{config.cache.audio_format}").touch()

    await asyncio.sleep(1.0)  # let subscribers come up
    now = time.time()
    messages = [
        ("alice", "Theme: songs everyone knows"),
        ("alice", f"kicking us off: https://youtu.be/{demo_videos[0]}"),
        ("bob", f"https://music.youtube.com/watch?v={demo_videos[1]}&si=xyz"),
        ("carol", f"this one https://www.youtube.com/watch?v={demo_videos[2]}"),
    ]
    for i, (sender, text) in enumerate(messages):
        await ingest.handle_message(sender=sender, text=text, ts=now + i * 60, source="mesh")
        await asyncio.sleep(0.5)
    log.info("demo seed complete")


async def run(config, demo: bool = False) -> None:
    log.info("meshradio %s starting (profile=%s)", __version__, config.hardware_profile)
    config.data_dir.mkdir(parents=True, exist_ok=True)

    # Snapshot the DB before migrations touch it — a bad migration then has a
    # clean pre-migration copy to roll back to. No-op on first boot (no DB yet).
    if config.backup.enabled and config.db_path.exists():
        try:
            dest = await asyncio.to_thread(
                backup_mod.snapshot, config.db_path, config.backup_dir, "premigrate"
            )
            if dest is not None:
                log.info("pre-migration db backup -> %s", dest.name)
                await asyncio.to_thread(backup_mod.rotate, config.backup_dir, config.backup.keep)
        except Exception:
            log.exception("pre-migration backup failed (continuing)")

    db = Database(config.db_path)
    await db.connect()
    bus = EventBus()

    ingest = IngestService(
        db, bus, channel=config.corescope.channel, tz=config.player.timezone
    )

    router = make_router(config.hardware_profile, bus)
    player = PlayerService(
        config.player,
        db,
        bus,
        backend=make_backend(config.hardware_profile, config.player.backend),
        output_getter=router.current,
    )
    player.radio = RadioService(config.cache, db, bus)
    cacher = Cacher(
        config.cache, config.cache_dir, db, bus, embed=config.player.backend == "embed"
    )
    panel = make_panel(config.hardware_profile, bus, player, router)
    power = (
        UpsPowerMonitor(bus) if config.hardware_profile == "pi4" else StaticPowerMonitor(bus)
    )

    services = [player, cacher, panel, power]
    if config.mesh.enabled:
        services.append(MeshIngest(config.mesh, ingest, bus))
    if config.corescope.enabled:
        services.append(CoreScopePoller(config.corescope, ingest, db, bus))
    if config.letsmesh.enabled and config.letsmesh.base_url:
        # Backup channel feed: the LetsMesh analyzer, same CoreScope API. Its
        # own cursor/status ("letsmesh"); overlapping messages dedupe against
        # the primary feed and the mesh node, so it's a no-op when CoreScope
        # is healthy and keeps the archive alive when it isn't.
        services.append(
            CoreScopePoller(
                config.letsmesh, ingest, db, bus, name="letsmesh", source="letsmesh"
            )
        )
    if config.relay.push_url and config.relay.token:
        services.append(RelayPusher(config.relay, db, tz=config.player.timezone))
    if config.backup.enabled:
        services.append(BackupService(config.backup, config.db_path, config.backup_dir))

    for service in services:
        service.start()

    demo_task = spawn("demo-seed", seed_demo(config, ingest)) if demo else None

    # Public embed hosting: every visiting browser gets its own session
    # player (queue/position/day), so nobody can pause or steal anyone
    # else's music. The appliance modes stay one communal radio.
    player_factory = None
    if isinstance(player.backend, EmbedBackend):
        def player_factory(out_bus: EventBus) -> PlayerService:
            p = PlayerService(
                config.player,
                db,
                bus,                       # hears shared TRACK_READY events
                backend=EmbedBackend(),
                output_getter=lambda: "embed",
                events_out=out_bus,        # announces state only to its session
            )
            p.start()
            return p

    web_app = create_app(
        bus,
        db,
        player,
        router,
        ingest=ingest,
        ingest_token=config.web.ingest_token,
        player_factory=player_factory,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            web_app, host=config.web.host, port=config.web.port, log_level="warning"
        )
    )
    log.info("web UI on http://%s:%d", config.web.host, config.web.port)
    try:
        await server.serve()
    finally:
        if demo_task:
            demo_task.cancel()
        for service in reversed(services):
            await service.stop()
        await db.close()
        log.info("meshradio stopped")


def main() -> None:
    parser = argparse.ArgumentParser(prog="meshradio", description="MeshRadio appliance")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--profile", choices=("dev", "pi4", "lite"), help="override hardware_profile")
    parser.add_argument("--port", type=int, help="override web port")
    parser.add_argument("--demo", action="store_true", help="seed fake channel traffic (dev)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Mesh sender names and theme titles carry emoji; keep Windows dev
    # consoles (cp1252) from raising on every log line that includes one.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("aiosqlite").setLevel(logging.INFO)

    config = load_config(args.config)
    if args.profile:
        config.hardware_profile = args.profile
    if args.port:
        config.web.port = args.port

    try:
        asyncio.run(run(config, demo=args.demo))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
