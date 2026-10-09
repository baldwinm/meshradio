"""What /healthz tells the uptime check beyond "up": failures that only
reached the logs, and how long since the Pi relay last pushed."""

import asyncio
import contextlib

import httpx

from meshradio.bus import EventBus
from meshradio.runtime import _errors, recent_errors, spawn, supervise

from .helpers import client_for, page_app, relay_embed_app


async def test_a_crashed_loop_and_a_failed_task_are_counted(monkeypatch):
    monkeypatch.setattr("meshradio.runtime._BACKOFF_S", (0,))
    runs = []
    settled = asyncio.Event()

    async def flaky():
        runs.append(1)
        if len(runs) < 3:
            raise RuntimeError("boom")
        settled.set()
        await asyncio.sleep(3600)

    task = supervise("flaky", flaky)
    await asyncio.wait_for(settled.wait(), 2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    async def doomed():
        raise ValueError("kaput")

    failed = spawn("doomed", doomed())
    with contextlib.suppress(ValueError):
        await failed
    await asyncio.sleep(0)  # the done-callback
    assert recent_errors() == {"flaky": 2, "doomed": 1}


def test_a_failing_bus_listener_is_counted():
    bus = EventBus()

    def broken(topic, payload):
        raise RuntimeError("listener bug")

    bus.listen(broken)
    bus.publish("some.topic")
    assert recent_errors() == {"listener:some.topic": 1}


def test_only_the_window_counts(monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr("meshradio.runtime.time.time", lambda: now)
    _errors.append((now - 3601, "old"))
    _errors.append((now - 10, "new"))
    assert recent_errors(3600) == {"new": 1}


async def test_a_request_that_raises_is_counted_and_reported(db, bus):
    app = page_app(db, bus)

    async def boom():
        raise RuntimeError("route bug")

    app.add_api_route("/boom", boom)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await client.get("/healthz")).json()["errors_1h"] == 0
        assert (await client.get("/boom")).status_code == 500
        body = (await client.get("/healthz")).json()
    assert body["errors_1h"] == 1
    assert body["error_sources"] == ["request"]


async def test_a_handled_error_is_not_counted(db, bus):
    """A 404 or a refused press is the app answering, not failing."""
    async with client_for(page_app(db, bus)) as client:
        assert (await client.get("/archive/1999-01-01")).status_code == 404
        assert (await client.get("/healthz")).json()["errors_1h"] == 0


async def test_healthz_reports_the_relays_last_push(db, bus):
    app = relay_embed_app(db, bus)
    async with client_for(app, visited=False) as client:
        assert (await client.get("/healthz")).json()["relay_age_s"] is None
        resp = await client.post(
            "/api/ingest", json={"messages": []},
            headers={"Authorization": "Bearer s3cret"},
        )
        assert resp.status_code == 200
        age = (await client.get("/healthz")).json()["relay_age_s"]
    assert age is not None and age < 5


async def test_the_generated_api_docs_are_off(db, bus):
    async with client_for(page_app(db, bus), visited=False) as client:
        for path in ("/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"):
            assert (await client.get(path)).status_code == 404, path
