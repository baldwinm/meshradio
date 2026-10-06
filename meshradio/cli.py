"""The ``meshradio`` command: start the radio, or run one maintenance task
and exit (backups, theme and track fixes, the feed probe).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import logging
import sqlite3
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import backup as backup_mod
from .app import run
from .config import ConfigError, load_config, validate_config
from .db import MAX_TITLE, VIDEO_ID_RE, Database, clean_text
from .ingest import parse
from .ingest.corescope import format_probe, probe
from .media.cacher import drop_cache_file
from .net import http_client
from .web.admin_auth import hash_password, new_totp_secret, otpauth_uri

log = logging.getLogger("meshradio")


def main() -> None:
    parser = argparse.ArgumentParser(prog="meshradio", description="MeshRadio appliance")
    parser.add_argument("--config", help="path to config.toml")
    parser.add_argument("--profile", choices=("dev", "pi4", "lite"),
                        help="override hardware_profile")
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
    parser.add_argument("--hash-admin-password", action="store_true",
                        help="ask for an admin password and print the hash to set as "
                             "MESHRADIO_ADMIN_PASSWORD_HASH (or [web] admin_password_hash); "
                             "the admin page at /admin exists only once one is set")
    parser.add_argument("--new-totp-secret", action="store_true",
                        help="print a new secret for two-step admin sign-in, to set as "
                             "MESHRADIO_ADMIN_TOTP_SECRET and add to an authenticator app")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    # Neither needs a config: they print a value for the operator to set.
    if args.hash_admin_password:
        raise SystemExit(_run_hash_admin_password())
    if args.new_totp_secret:
        secret = new_totp_secret()
        print(secret)
        print(f"authenticator link: {otpauth_uri(secret)}")
        raise SystemExit(0)

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

    # A value the radio can't run on stops it here, with every offending key
    # named, rather than as a loop crashing under the supervisor forever.
    try:
        config = load_config(args.config)
        if args.profile:
            config.hardware_profile = args.profile
        if args.port:
            config.web.port = args.port
            validate_config(config)
    except ConfigError as exc:
        print(f"meshradio: {exc}", file=sys.stderr)
        raise SystemExit(2) from None

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

    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(config, demo=args.demo))


def _run_hash_admin_password() -> int:
    """Handle --hash-admin-password: read a password twice, print its hash."""
    password = getpass.getpass("Admin password: ")
    if len(password) < 12:
        print("use at least 12 characters", file=sys.stderr)
        return 1
    if getpass.getpass("Again: ") != password:
        print("the two didn't match", file=sys.stderr)
        return 1
    print(hash_password(password))
    print("Set that as MESHRADIO_ADMIN_PASSWORD_HASH (Render: Environment; the Pi: "
          "/etc/meshradio/env) and restart. Setting a new one signs every browser out.",
          file=sys.stderr)
    return 0


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
    title = clean_text(args.set_theme, MAX_TITLE)   # the archive's own bounds
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
            await db.log_admin("create_theme", actor="cli", target=date, after=title)
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
        await db.log_admin(
            "rename_theme", actor="cli", target=date,
            before=existing["title"], after=theme["title"],
            undo={"theme_id": theme["id"], "title": existing["title"]},
        )
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
            await db.log_admin(
                "remove_track", actor="cli", target=date, before=label, after="removed",
                undo={"date": date, "video_id": deleted["video_id"]},
            )
            print(f"{date}: removed {label} ({deleted['video_id']}, "
                  f"shared by {deleted['sender']})")
            dropped = drop_cache_file(config.cache_dir, deleted)
            if dropped is not None:
                print(f"  cached audio removed ({dropped.name})")
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
        for line in format_probe(report):
            print(f"  {line}")
        if not report.ok:
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    main()
