"""Builders the test modules share: apps, clients, seeded tracks, and the
small instruments (a call counter, a hand-driven WebSocket) that let a test
watch the server without a running loop of its own. The ``db`` and ``bus``
fixtures live in conftest.py."""

import asyncio
import json
import time
from contextlib import asynccontextmanager

import httpx

from meshradio.audio.routing import make_router
from meshradio.bus import EventBus
from meshradio.config import Config, PlayerConfig
from meshradio.db import Database
from meshradio.ingest.service import IngestService
from meshradio.media.player import EmbedBackend, NullBackend, PlayerService
from meshradio.web.admin_auth import AdminSettings, csrf_token, hash_password
from meshradio.web.server import create_app

# -- tracks -------------------------------------------------------------------


async def seed_day(db, date, video_id):
    """A day with a theme and one pending track: enough for a page to list."""
    theme = await db.create_theme(date, f"theme {date}")
    await db.add_track(
        video_id=video_id, url="u", channel="#music", sender="alice",
        mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
    )


async def share(db, date, video_id, sender, title="Song", artist=None, theme=None,
                set_by=None, source="mesh"):
    """One song posted to a day (creating the day's theme on first use)."""
    row = await db.create_theme(date, theme or f"theme {date}", set_by=set_by)
    track = await db.add_track(
        video_id=video_id, url="u", channel="#music", sender=sender,
        mesh_ts=time.time(), source=source, theme_id=row["id"],
    )
    await db.update_track_metadata(track["id"], title=title, artist=artist, duration=60)
    return await db.track_by_id(track["id"])


async def seed_shares(db, video_id, days, title=None, artist=None, senders=("alice",)):
    """One song posted on several archive days — the repeat share the search
    page collapses into a single row. Each day gets its own theme because
    ``add_track`` dedupes a same-day repost, so a song can only be shared
    again on another day. ``senders`` cycles, for the several-members case.

    The shares are an hour apart and end at now: the same message arriving
    twice is one dedupe bucket of sixty seconds, so shares closer than that
    by one member would collapse into one, and a timestamp in the future
    sits outside the player's live window."""
    tracks = []
    span = len(days) - 1
    for i, date in enumerate(days):
        theme = await db.create_theme(date, f"theme {date}")
        track = await db.add_track(
            video_id=video_id, url=f"https://www.youtube.com/watch?v={video_id}",
            channel="#music", sender=senders[i % len(senders)],
            mesh_ts=time.time() - (span - i) * 3600, source="mesh", theme_id=theme["id"],
        )
        await db.update_track_metadata(track["id"], title=title or video_id, artist=artist)
        tracks.append(track)
    return tracks


async def make_ready_track(db: Database, video_id: str, duration: float = 0.05):
    """A cached, titled track on 2026-07-06, posted just now. (A fixed
    timestamp here aged past the player's live window mid-session once and
    failed half the suite.)"""
    return await make_ready_on(db, video_id, "2026-07-06", duration=duration, title=video_id)


async def make_ready_on(db, video_id, date, duration=60, title=None):
    """A ready track filed under a specific archive day."""
    theme = await db.create_theme(date, f"theme {date}")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music", sender="alice", mesh_ts=time.time(),
        source="mesh", theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title=title or video_id, duration=duration)
    await db.set_cache_status(track["id"], "ready", f"/cache/{video_id}.opus")
    return await db.track_by_id(track["id"])


async def make_pending_track(db: Database, video_id: str):
    """A channel track that never got a cached file or metadata — the state
    most tracks sit in on the datacenter-hosted embed instance (oEmbed
    throttled). Streamable by id all the same."""
    theme = await db.create_theme("2026-07-06", "test theme")
    track = await db.add_track(
        video_id=video_id,
        url=f"https://www.youtube.com/watch?v={video_id}",
        channel="#music",
        sender="alice",
        mesh_ts=time.time(),
        source="mesh",
        theme_id=theme["id"],
    )
    return await db.track_by_id(track["id"])


# -- players and apps ---------------------------------------------------------


def make_player(db, bus, **config_overrides) -> PlayerService:
    return PlayerService(PlayerConfig(**config_overrides), db, bus, backend=NullBackend())


def make_embed_player(db, bus, **config_overrides) -> PlayerService:
    """A player whose backend streams in the browser — the public-hosting mode
    where tracks are playable by video id without a downloaded file."""
    return PlayerService(PlayerConfig(**config_overrides), db, bus, backend=EmbedBackend())


def page_app(db, bus, **kwargs):
    """The appliance: one communal player, no sessions."""
    return create_app(bus, db, make_player(db, bus), make_router("dev", bus), **kwargs)


def embed_app(db, bus, **kwargs):
    """The hosted deployment: a player per visitor session."""
    def factory(out_bus: EventBus) -> PlayerService:
        return PlayerService(
            PlayerConfig(), db, bus, backend=EmbedBackend(), events_out=out_bus
        )

    return create_app(
        bus, db, make_embed_player(db, bus), make_router("dev", bus),
        player_factory=factory, **kwargs,
    )


def relay_embed_app(db, bus, token="s3cret"):
    """The hosted deployment with the relay receiver on, so a push lands in
    the rows every visitor is cued onto."""
    ingest = IngestService(db, bus, channel="#music")
    return embed_app(db, bus, ingest=ingest, ingest_token=token)


@asynccontextmanager
async def client_for(app, visited=True):
    """A browser. By default it has loaded a page before the test starts
    pressing things — that is where a real one gets its session cookie, and
    a press that arrives without one opens nothing (see
    ``WebContext.get_player``). ``visited=False`` is the first visit itself."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        if visited:
            await client.get("/")
        yield client


def peer(app, host):
    """A client arriving from ``host``, for the proxy and rate-limit rules."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=(host, 1)), base_url="http://test"
    )


def sid_of(client) -> str:
    """The session key a client's cookie names: the id without its signature."""
    return client.cookies["mr_sid"].split(".")[0]


async def cookie_for(app) -> str:
    """The signed session cookie a first page view hands out."""
    async with client_for(app, visited=False) as client:
        return (await client.get("/")).cookies["mr_sid"]


# -- instruments --------------------------------------------------------------


def counting(db, name):
    """Count calls to one Database method (and keep it working)."""
    calls = {"n": 0}
    original = getattr(db, name)

    async def wrapped(*args, **kwargs):
        calls["n"] += 1
        return await original(*args, **kwargs)

    setattr(db, name, wrapped)
    return calls


class Socket:
    """One hand-driven WebSocket client over the ASGI app, kept open until
    ``close()``. (The test client would run the app on another thread and
    loop, away from the fixture's database.)"""

    def __init__(self, app, cookie=None, origin=None):
        self.app = app
        headers = [(b"host", b"test")]
        if cookie is not None:
            headers.append((b"cookie", f"mr_sid={cookie}".encode()))
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        self.scope = {
            "type": "websocket", "asgi": {"version": "3.0"}, "http_version": "1.1",
            "scheme": "ws", "path": "/ws", "raw_path": b"/ws", "root_path": "",
            "query_string": b"", "headers": headers, "client": ("1.2.3.4", 5),
            "server": ("test", 80), "subprotocols": [],
            "extensions": {"websocket.http.response": {}},
        }
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: list[dict] = []
        self.task: asyncio.Task | None = None

    async def open(self):
        await self.inbox.put({"type": "websocket.connect"})
        self.task = asyncio.create_task(self.app(self.scope, self.inbox.get, self._send))
        # Wait for the handshake's answer rather than a fixed time: the first
        # one writes the session secret, which a slow CI runner can take past
        # any fixed sleep. Then a moment more for the first push.
        deadline = asyncio.get_running_loop().time() + 2
        while not self.sent and not self.task.done():
            if asyncio.get_running_loop().time() > deadline:
                break
            await asyncio.sleep(0.005)
        await asyncio.sleep(0.05)
        return self

    async def _send(self, message):
        self.sent.append(message)

    @property
    def accepted(self) -> bool:
        return bool(self.sent) and self.sent[0]["type"] == "websocket.accept"

    @property
    def refused_with(self):
        """The close code if the handshake was refused before accept."""
        if self.sent and self.sent[0]["type"] == "websocket.close":
            return self.sent[0]["code"]
        return None

    def states(self) -> list[dict]:
        return [json.loads(m["text"]) for m in self.sent if m["type"] == "websocket.send"]

    async def say(self, text: str):
        await self.inbox.put({"type": "websocket.receive", "text": text})
        await asyncio.sleep(0.05)

    async def close(self):
        await self.inbox.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(self.task, 2)
        await asyncio.sleep(0.02)              # the leave broadcast to the others


# -- the admin page -----------------------------------------------------------

ADMIN_PASSWORD = "correct horse battery"


def admin_settings(tmp_path, totp_secret="", **config_overrides):
    """Admin on, with a cheap hash (scrypt at N=16: the real cost is for
    production, not for a test that signs in forty times) and a config whose
    data directory is the test's own."""
    config = Config(data_dir=tmp_path)
    for key, value in config_overrides.items():
        section, name = key.split("__")
        setattr(getattr(config, section), name, value)
    return AdminSettings(
        password_hash=hash_password(ADMIN_PASSWORD, n=16),
        totp_secret=totp_secret,
        config=config,
    )


async def sign_in(client, password=ADMIN_PASSWORD, code=None):
    """Post the sign-in form; returns the response (a 303 on success)."""
    data = {"password": password}
    if code is not None:
        data["code"] = code
    return await client.post("/admin/login", data=data)


def csrf_for(client) -> str:
    return csrf_token(client.cookies["mr_admin"])


async def admin_post(client, path, **data):
    """A signed-in admin's form post, CSRF token included."""
    return await client.post(path, data={"csrf": csrf_for(client), **data})
