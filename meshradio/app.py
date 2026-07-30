"""MeshRadio entrypoint — one asyncio process, modules wired over the bus.

Startup order: DB → bus → ingest (mesh + CoreScope) → cacher → player →
routing → panel → power → web. Everything is a task in one loop; systemd
manages the process (architecture §4).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

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
    parser.add_argument("--list-backups", action="store_true",
                        help="list archive DB backup snapshots and exit")
    parser.add_argument("--restore-backup", metavar="WHICH",
                        help="restore the archive DB from a snapshot ('latest', a filename in "
                             "the backup dir, or a path) and exit; the current DB is snapshotted "
                             "first so it's reversible. Stop the service before running.")
    parser.add_argument("--set-theme", metavar="TITLE",
                        help="retitle a day's theme in this archive and exit. For fixing a "
                             "title the channel got wrong — ingest locks the day's theme on "
                             "the first 'Theme:' post, so a corrected repost is ignored. "
                             "Defaults to today; see --theme-date.")
    parser.add_argument("--theme-date", metavar="YYYY-MM-DD",
                        help="which day --set-theme applies to (default: today, in the "
                             "configured player timezone)")
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

    if args.list_backups or args.restore_backup is not None:
        raise SystemExit(_run_backup_cli(config, args))

    if args.set_theme is not None:
        raise SystemExit(asyncio.run(_run_set_theme(config, args)))
    if args.theme_date is not None:
        parser.error("--theme-date only applies with --set-theme")

    try:
        asyncio.run(run(config, demo=args.demo))
    except KeyboardInterrupt:
        pass


def _run_backup_cli(config, args) -> int:
    """Handle --list-backups / --restore-backup, then exit (no server)."""
    if args.list_backups:
        snaps = backup_mod.list_snapshots(config.backup_dir)
        if not snaps:
            print(f"no backups in {config.backup_dir}")
            return 0
        print(f"backups in {config.backup_dir} (newest last):")
        for s in snaps:
            print(f"  {s.name}  ({s.stat().st_size:,} bytes)")
        return 0

    snap = backup_mod.resolve_snapshot(config.backup_dir, args.restore_backup)
    if snap is None:
        print(f"no backup matching {args.restore_backup!r} in {config.backup_dir}", file=sys.stderr)
        return 1
    try:
        safety = backup_mod.restore(config.db_path, snap, config.backup_dir)
    except (ValueError, FileNotFoundError) as exc:
        print(f"restore failed: {exc}", file=sys.stderr)
        return 1
    print(f"restored {config.db_path} from {snap.name}")
    if safety is not None:
        print(f"previous DB saved as {safety.name} — restore it to undo")
    print("start the service to load the restored archive.")
    return 0


async def _run_set_theme(config, args) -> int:
    """Handle --set-theme: retitle (or create) a day's theme, then exit.

    Acts on whatever archive this config points at, so it has to be run once
    per instance you want corrected. A rename does *not* propagate over the
    relay to a hosted receiver: the receiver's own theme for that day is
    locked, so it ignores the replayed theme message the same way it ignores a
    corrected repost on the channel."""
    title = args.set_theme.strip()
    if not title:
        print("--set-theme needs a non-empty title", file=sys.stderr)
        return 1

    if args.theme_date:
        try:
            date = datetime.strptime(args.theme_date, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            print(f"--theme-date must be YYYY-MM-DD, got {args.theme_date!r}", file=sys.stderr)
            return 1
    else:
        date = datetime.now(ZoneInfo(config.player.timezone)).strftime("%Y-%m-%d")

    db = Database(config.db_path)
    await db.connect()
    try:
        existing = await db.latest_theme_for_date(date)
        if existing is None:
            # No posts that day yet. Open the playlist with this title so links
            # arriving later attach to it instead of an "Untitled —" placeholder.
            theme = await db.create_theme(
                date, title, raw_message=f"Theme: {title}", locked=True
            )
            print(f"{date}: no theme existed — created {theme['title']!r}")
            return 0
        if existing["title"] == title:
            print(f"{date}: theme is already {title!r} — nothing to do")
            return 0
        try:
            theme = await db.rename_theme(existing["id"], title)
        except sqlite3.IntegrityError:
            print(f"{date} already has a different theme row titled {title!r}; "
                  f"rename or remove it first", file=sys.stderr)
            return 1
        count = len(await db.tracks_for_theme(theme["id"]))
        plural = "" if count == 1 else "s"
        print(f"{date}: {existing['title']!r} -> {theme['title']!r} ({count} song{plural} kept)")
        print("Reload the site to see it. Run this on each instance (relay hosts "
              "keep their own locked theme and won't pick this up).")
        return 0
    finally:
        await db.close()


if __name__ == "__main__":
    main()
