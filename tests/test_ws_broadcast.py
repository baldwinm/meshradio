"""State fan-out to a session's open pages: all at once, and no page can
hold the others up."""

import asyncio
import time

from meshradio.config import PlayerConfig
from meshradio.media.player import NullBackend, PlayerService
from meshradio.web import ws as ws_mod
from meshradio.web.sessions import SpeakerRegistry


class Conn:
    def __init__(self, delay=0.0, fail=False):
        self.delay, self.fail, self.got = delay, fail, []

    async def send_json(self, data):
        if self.fail:
            raise RuntimeError("closed")
        await asyncio.sleep(self.delay)
        self.got.append(data)


async def test_broadcast_sends_in_parallel_and_survives_a_dead_socket(db, bus):
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    reg = SpeakerRegistry()
    slow, quick, dead = Conn(delay=0.2), Conn(), Conn(fail=True)
    for c in (slow, quick, dead):
        reg.join(c)
    started = time.perf_counter()
    await ws_mod.broadcast_state(reg, player)
    assert time.perf_counter() - started < 0.35                # not 0.2 per socket in turn
    assert quick.got and slow.got                              # both got the state
    assert quick.got[0]["topic"] == "player.state"
    assert slow.got[0]["data"]["speaker"] is False and quick.got[0]["data"]["speaker"] is False
    assert dead.got == [] and reg.is_speaker(dead)             # newest joined; broadcast is unaffected


async def test_broadcast_abandons_a_socket_that_never_reads(db, bus, monkeypatch):
    monkeypatch.setattr(ws_mod, "SEND_TIMEOUT_S", 0.05)
    player = PlayerService(PlayerConfig(), db, bus, backend=NullBackend())
    reg = SpeakerRegistry()
    stuck, fine = Conn(delay=10), Conn()
    reg.join(stuck)
    reg.join(fine)
    started = time.perf_counter()
    await ws_mod.broadcast_state(reg, player)
    assert time.perf_counter() - started < 1
    assert fine.got and not stuck.got
