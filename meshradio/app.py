"""MeshRadio entrypoint — one asyncio process, modules wired over the bus.

Startup order: DB → bus → ingest (mesh + CoreScope + backup feed) → cacher →
player → routing → panel → power → web. Everything is a task in one loop; systemd
manages the process (architecture §4).
"""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import __version__, config_overrides
from . import backup as backup_mod
from .audio.routing import make_router
from .backup import BackupService
from .bus import EventBus
from .config_overrides import SETTINGS, current
from .db import Database
from .ingest.corescope import CoreScopePoller
from .ingest.mesh import MeshIngest
from .ingest.relay import RelayPusher
from .ingest.service import IngestService
from .media.cacher import Cacher, ytdlp_version
from .media.durations import DurationService
from .media.player import EmbedBackend, MpvBackend, NullBackend, PlayerService, WebBackend
from .media.radio import RadioService
from .runtime import spawn
from .system.power import StaticPowerMonitor, UpsPowerMonitor
from .ui.panel import make_panel
from .web.admin_auth import AdminSettings
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
    # Settings changed on the admin page, laid over the file's before
    # anything reads them (§9).
    file_values = {s.key: current(config, s) for s in SETTINGS}
    overrides = config_overrides.apply_saved(config, await config_overrides.load(db))
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
    # Embed hosting is a website, not a device: no front-panel stand-in logging
    # "now playing" for the shared player nobody is listening to.
    panel = make_panel(
        config.hardware_profile, bus, player, router,
        dev_log=not isinstance(player.backend, EmbedBackend),
    )
    power = (
        UpsPowerMonitor(bus) if config.hardware_profile == "pi4" else StaticPowerMonitor(bus)
    )

    services = [player, cacher, power]
    if panel is not None:
        services.insert(2, panel)
    # Embed hosting never downloads, so song lengths are looked up instead.
    if isinstance(player.backend, EmbedBackend):
        services.append(DurationService(db, bus))
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
    relay = None
    if config.relay.push_url and config.relay.token:
        try:
            relay = RelayPusher(config.relay, db, tz=config.player.timezone)
            services.append(relay)
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

    # The admin page exists only with a password hash configured (§9).
    admin = None
    if config.web.admin_password_hash:
        admin = AdminSettings(
            password_hash=config.web.admin_password_hash,
            totp_secret=config.web.admin_totp_secret,
            config=config,
            relay=relay,
            file_values=file_values,
            overrides=overrides,
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
        trusted_proxies=config.web.trusted_proxies,
        proxy_hops=config.web.proxy_hops,
        rate_limit=config.web.rate_limit,
        admin=admin,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            web_app, host=config.web.host, port=config.web.port, log_level="warning",
            # The same list gates uvicorn's own reading of X-Forwarded-For
            # and -Proto, so request.client and request.url.scheme are the
            # visitor's when the proxy is trusted and the peer's when not.
            # With [web] proxy_hops set, web/proxy.py does this instead,
            # counting from the right where a visitor can't forge entries.
            proxy_headers=not config.web.proxy_hops,
            forwarded_allow_ips=",".join(config.web.trusted_proxies) or "127.0.0.1",
        )
    )
    log.info("web UI on http://%s:%d", config.web.host, config.web.port)
    if not isinstance(player.backend, EmbedBackend):
        # Off the request path: a slow or missing binary must not hold the
        # server up. Embed hosting never runs yt-dlp.
        spawn(
            "ytdlp-version",
            _note_ytdlp_version(config.cache.ytdlp_bin, web_app.state.ctx.health),
        )
    try:
        await server.serve()
    finally:
        if demo_task:
            demo_task.cancel()
        for service in reversed(services):
            await service.stop()
        await db.close()
        log.info("meshradio stopped")


async def _note_ytdlp_version(binary: str, health: dict) -> None:
    """Record yt-dlp's version for /healthz and the log — the one place the
    nightly update timer's work can be seen."""
    version = await ytdlp_version(binary)
    health["ytdlp_version"] = version
    if version:
        log.info("yt-dlp %s (%s)", version, binary)
    else:
        log.warning("yt-dlp not found or not runnable (%s); downloads will fail", binary)


if __name__ == "__main__":
    from .cli import main

    main()
