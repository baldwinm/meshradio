"""MeshRadio entrypoint — one asyncio process, modules wired over the bus.

Startup order: DB → bus → ingest (mesh + CoreScope + backup feed) → cacher →
player → routing → panel → power → web. Everything is a task in one loop; systemd
manages the process (architecture §4).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn

from . import __version__
from .audio.routing import make_router
from . import backup as backup_mod
from .backup import BackupService
from .bus import EventBus
from .config import load_config
from .db import Database, VIDEO_ID_RE
from .ingest import parse
from .ingest.corescope import CoreScopePoller, probe
from .ingest.mesh import MeshIngest
from .ingest.relay import RelayPusher
from .ingest.service import IngestService
from .media.cacher import Cacher
from .media.player import EmbedBackend, MpvBackend, NullBackend, PlayerService, WebBackend
from .media.radio import RadioService
from .net import http_client
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
    # Backup analyzer feed, polled alongside the primary rather than failed
    # over to: dedupe no-ops the overlap while both are up, so an outage on
    # either one costs nothing but the other's poll interval.
    if config.comchan.enabled and config.comchan.base_url:
        services.append(
            CoreScopePoller(
                config.comchan, ingest, db, bus, name="comchan", source="comchan"
            )
        )
    if config.relay.push_url and config.relay.token:
        try:
            services.append(RelayPusher(config.relay, db, tz=config.player.timezone))
        except ValueError as exc:
            # A misconfigured relay must not take the radio down with it (or
            # loop the systemd unit); it just doesn't push until fixed.
            log.error("relay disabled: %s", exc)
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
            # Not started here: the SessionManager starts the players it
            # keeps and never starts the throwaway one a session-less page
            # view renders from.
            return PlayerService(
                config.player,
                db,
                bus,                       # hears shared TRACK_READY events
                backend=EmbedBackend(),
                output_getter=lambda: "embed",
                events_out=out_bus,        # announces state only to its session
            )

    web_app = create_app(
        bus,
        db,
        player,
        router,
        ingest=ingest,
        ingest_token=config.web.ingest_token,
        player_factory=player_factory,
        allowed_hosts=config.web.allowed_hosts,
        public_url=config.web.public_url,
        security_headers=config.web.security_headers,
        csp_report_only=config.web.csp_report_only,
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
    parser.add_argument("--delete-track", metavar="VIDEO",
                        help="remove one song from a day's playlist and exit — a YouTube "
                             "URL or a bare video id. For a song that shouldn't be in the "
                             "archive at all (posted before the theme was set, posted to "
                             "the wrong day); a repost can't undo it and neither can the "
                             "queue's Remove button. The removal is remembered, so a "
                             "re-backfill can't put it back. Defaults to today; see "
                             "--track-date.")
    parser.add_argument("--track-date", metavar="YYYY-MM-DD",
                        help="which day --delete-track applies to (default: today, in the "
                             "configured player timezone)")
    parser.add_argument("--probe-feed", metavar="FEED", nargs="?", const="all",
                        choices=("all", "corescope", "comchan"),
                        help="poll an analyzer feed once from this machine and exit, "
                             "without writing to the archive: is it reachable, does it "
                             "list the channel, and does its API still parse? "
                             "'corescope', 'comchan', or both when the name is left off. "
                             "Exits non-zero if a probed feed fails.")
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
    if args.delete_track is not None:
        raise SystemExit(asyncio.run(_run_delete_track(config, args)))
    if args.track_date is not None:
        parser.error("--track-date only applies with --delete-track")
    if args.probe_feed is not None:
        raise SystemExit(asyncio.run(_run_probe_feed(config, args)))

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

    date = _resolve_day(args.theme_date, config, "--theme-date")
    if date is None:
        return 1

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


def _resolve_day(value: str | None, config, flag: str) -> str | None:
    """A YYYY-MM-DD flag value, or today in the configured timezone. None on a
    malformed date (the caller reports and exits)."""
    if not value:
        return datetime.now(ZoneInfo(config.player.timezone)).strftime("%Y-%m-%d")
    try:
        return datetime.strptime(value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        print(f"{flag} must be YYYY-MM-DD, got {value!r}", file=sys.stderr)
        return None


async def _run_delete_track(config, args) -> int:
    """Handle --delete-track: drop one song from a day's playlist, then exit.

    The song stays gone: a tombstone tells ingest to ignore the link if the
    channel replays it (a relay re-backfill, a restore, a repost). Like
    --set-theme this edits one archive, so run it on each instance you want
    fixed — the hosted receiver and the Pi keep their own."""
    raw = args.delete_track.strip()
    links = parse.extract_links(raw)
    video_id = links[0].video_id if links else raw
    if not VIDEO_ID_RE.match(video_id):   # the same id the archive would store
        print(f"--delete-track needs a YouTube link or an 11-character video id, "
              f"got {raw!r}", file=sys.stderr)
        return 1

    date = _resolve_day(args.track_date, config, "--track-date")
    if date is None:
        return 1

    db = Database(config.db_path)
    await db.connect()
    try:
        tracks = await db.tracks_for_day(date)
        matches = [t for t in tracks if t["video_id"] == video_id]
        if not matches:
            print(f"{date}: no song with video id {video_id} in the archive", file=sys.stderr)
            if tracks:
                print("that day's songs:", file=sys.stderr)
                for t in tracks:
                    label = t["title"] or t["url"]
                    print(f"  {t['video_id']}  {label} (shared by {t['sender']})",
                          file=sys.stderr)
            return 1

        for track in matches:
            theme_id = track["theme_id"]
            deleted = await db.delete_track(track["id"])
            assert deleted is not None
            label = deleted["title"] or deleted["url"]
            print(f"{date}: removed {label} ({deleted['video_id']}, "
                  f"shared by {deleted['sender']})")
            _drop_cache_file(config, deleted)
            if theme_id is not None and await db.delete_empty_placeholder(theme_id):
                print(f"{date}: the day's empty 'Untitled —' placeholder went with it")
        print("It won't come back: a replayed channel message for it is now ignored, "
              "the way a corrected repost is.")
        print("Run this on each instance you want fixed (relay hosts keep their own "
              "archive). Restart the service if it's already sitting in a live queue.")
        return 0
    finally:
        await db.close()


async def _run_probe_feed(config, args) -> int:
    """Handle --probe-feed: one live request per analyzer feed, then exit.

    The poller logs a feed's failures but never shows what a working one is
    answering, and the hosts aren't reachable from every network — so this
    is the check to run from the machine that will do the polling. Reports
    whether the host answered, whether it lists the channel, which fields
    its messages carry, and the newest few posts as the poller would read
    them. The archive is never opened."""
    feeds = {"corescope": config.corescope, "comchan": config.comchan}
    chosen = list(feeds) if args.probe_feed == "all" else [args.probe_feed]
    failed = 0
    for name in chosen:
        feed = feeds[name]
        if not feed.base_url:
            print(f"{name}: no base_url configured — nothing to probe")
            if args.probe_feed != "all":
                failed += 1
            continue
        state = "" if feed.enabled else "  (enabled = false — the radio won't poll it)"
        print(f"{name}: {feed.base_url}  {feed.channel}{state}")
        async with http_client(base_url=feed.base_url) as client:
            report = await probe(client, feed.channel, name=name)
        for line in _format_probe(report):
            print(f"  {line}")
        if not report.ok:
            failed += 1
    return 1 if failed else 0


def _format_probe(report) -> list[str]:
    lines = []
    if report.channels_error:
        lines.append(f"channel listing failed: {report.channels_error}")
    elif report.channels:
        listed = ", ".join(report.channels[:12])
        more = f", … ({len(report.channels)} in all)" if len(report.channels) > 12 else ""
        lines.append(f"channels listed: {listed}{more}")
        if report.channel_listed is False:
            lines.append(f"{report.channel} is NOT among them — check [corescope]/[comchan] channel")
    if not report.ok and not report.served:
        lines.append(f"FAILED after {report.elapsed_s:.1f}s: {report.error}")
        return lines
    total = "unknown" if report.total is None else f"{report.total:,}"
    lines.append(f"messages: {total} on the analyzer; newest page served {report.served}, "
                 f"{report.parsed} parse")
    if report.fields:
        lines.append("fields: " + ", ".join(report.fields))
    if report.newest:
        lines.append("newest:")
        for msg in report.newest:
            text = msg["text"].replace("\n", " ")
            if len(text) > 72:
                text = text[:71] + "…"
            lines.append(f"  {msg['first_seen'] or '(no first_seen)'}  {msg['sender']}: {text}")
    if report.ok:
        lines.append(f"ok ({report.elapsed_s:.1f}s)")
    else:
        lines.append(f"FAILED: {report.error}")
    return lines


def _drop_cache_file(config, track: dict) -> None:
    """Delete the removed song's cached audio, if this node downloaded one.

    Best-effort and confined to the cache directory: a stale or hand-edited
    cache_path must not turn a track deletion into an arbitrary unlink."""
    path = track.get("cache_path")
    if not path:
        return
    cache_dir = Path(config.cache_dir).resolve()
    try:
        target = Path(path).resolve()
        target.relative_to(cache_dir)
        target.unlink()
    except (OSError, ValueError):
        return
    print(f"  cached audio removed ({target.name})")


if __name__ == "__main__":
    main()
