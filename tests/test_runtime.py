"""Supervised runtime: crashed service loops restart instead of dying silently."""

import asyncio
import logging

from meshradio.runtime import Service, spawn, supervise


async def test_supervise_restarts_after_crash(monkeypatch):
    monkeypatch.setattr("meshradio.runtime._BACKOFF_S", (0,))
    runs = []
    settled = asyncio.Event()

    async def flaky_loop():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("boom")
        settled.set()
        await asyncio.sleep(3600)

    task = supervise("flaky", flaky_loop)
    await asyncio.wait_for(settled.wait(), 2)
    assert len(runs) == 3           # crashed twice, restarted twice
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def test_supervise_clean_return_is_final():
    async def one_shot():
        return

    task = supervise("done", one_shot)
    await asyncio.wait_for(task, 1)  # completes; no restart loop


async def test_spawn_logs_unhandled_exception(caplog):
    async def boom():
        raise ValueError("kaput")

    with caplog.at_level(logging.ERROR, logger="meshradio.runtime"):
        task = spawn("doomed", boom())
        try:
            await task
        except ValueError:
            pass
        await asyncio.sleep(0)  # let the done-callback run
    assert "doomed" in caplog.text


async def test_service_start_stop():
    class Ticker(Service):
        def __init__(self):
            self.ticks = 0

        async def _run(self):
            while True:
                self.ticks += 1
                await asyncio.sleep(0.01)

    s = Ticker()
    s.start()
    await asyncio.sleep(0.05)
    await s.stop()
    assert s.ticks >= 1
    assert s._task is None


async def test_busy_timeout_pragma(db):
    cur = await db.db.execute("PRAGMA busy_timeout")
    (value,) = await cur.fetchone()
    assert value == 5000
