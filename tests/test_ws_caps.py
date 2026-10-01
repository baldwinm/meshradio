"""Ceilings on open WebSockets, and on how often a page may claim the speaker.

Every socket is two tasks and one more target for every state fan-out, and a
claim re-elects and broadcasts to every socket in the registry — so a bot
holding one cookie used to be able to open sockets without end, or send
"claim" in a loop and have the server fan state out to everyone on each one.
"""

import asyncio
import json

from meshradio.web import ws as ws_mod
from meshradio.web.sessions import MAX_SOCKETS_PER_SESSION, SpeakerRegistry

from .test_sessions import client_for, embed_app


class Socket:
    """One hand-driven WebSocket client over the ASGI app, kept open until
    ``close()`` (the test client would run the app on another thread and
    loop, away from the fixture's database)."""

    def __init__(self, app, cookie=None):
        self.app = app
        headers = [(b"host", b"test")]
        if cookie is not None:
            headers.append((b"cookie", f"mr_sid={cookie}".encode()))
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
        await asyncio.sleep(0.05)              # let the handshake and first push run
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


async def cookie_for(app) -> str:
    async with client_for(app, visited=False) as client:
        return (await client.get("/")).cookies["mr_sid"]


async def test_a_session_has_a_ceiling_on_open_sockets(db, bus):
    app = embed_app(db, bus)
    cookie = await cookie_for(app)
    socks = [await Socket(app, cookie).open() for _ in range(MAX_SOCKETS_PER_SESSION)]
    assert all(s.accepted for s in socks)
    extra = await Socket(app, cookie).open()
    assert extra.refused_with == 1013 and not extra.accepted   # try again later
    assert app.state.ctx.open_sockets == MAX_SOCKETS_PER_SESSION
    await socks[0].close()                                      # a slot frees up
    again = await Socket(app, cookie).open()
    assert again.accepted
    # Another visitor is unaffected: the ceiling is per session.
    other = await Socket(app, await cookie_for(app)).open()
    assert other.accepted
    for s in socks[1:] + [again, other]:
        await s.close()
    assert app.state.ctx.open_sockets == 0


async def test_the_process_has_a_ceiling_on_open_sockets(db, bus, monkeypatch):
    monkeypatch.setattr(ws_mod, "MAX_SOCKETS", 2)
    app = embed_app(db, bus)
    first = await Socket(app, await cookie_for(app)).open()
    second = await Socket(app, await cookie_for(app)).open()
    assert first.accepted and second.accepted
    third = await Socket(app, await cookie_for(app)).open()
    assert third.refused_with == 1013
    await first.close()
    fourth = await Socket(app, await cookie_for(app)).open()
    assert fourth.accepted
    await second.close()
    await fourth.close()
    assert app.state.ctx.open_sockets == 0


async def test_claims_from_the_speaker_or_too_fast_change_nothing(db, bus):
    app = embed_app(db, bus)
    cookie = await cookie_for(app)
    a = await Socket(app, cookie).open()
    b = await Socket(app, cookie).open()                 # newest: b is the speaker
    assert b.states()[-1]["data"]["speaker"] is True
    n_a, n_b = len(a.states()), len(b.states())
    await b.say("claim")                                 # already has the role: no fan-out
    assert (len(a.states()), len(b.states())) == (n_a, n_b)
    await a.say("claim")                                 # takes it: everyone hears
    assert (len(a.states()), len(b.states())) == (n_a + 1, n_b + 1)
    assert a.states()[-1]["data"]["speaker"] is True
    await b.say("claim")                                 # b's first claim: allowed
    assert (len(a.states()), len(b.states())) == (n_a + 2, n_b + 2)
    assert b.states()[-1]["data"]["speaker"] is True
    await a.say("claim")                                 # a claimed under a second ago: ignored
    assert (len(a.states()), len(b.states())) == (n_a + 2, n_b + 2)
    assert a.states()[-1]["data"]["speaker"] is False
    await a.say("not a claim")                           # anything else is ignored too
    assert len(a.states()) == n_a + 2
    await a.close()
    await b.close()


def test_registry_refuses_joins_past_its_ceiling():
    reg = SpeakerRegistry(max_clients=2)
    a, b, c = object(), object(), object()
    assert reg.join(a) and reg.join(b)
    assert not reg.join(c) and reg.full()
    assert reg.is_speaker(b) and c not in reg.clients()
    reg.leave(a)
    assert reg.join(c) and reg.is_speaker(c)
