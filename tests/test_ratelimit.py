"""Per-client ceilings on presses and searches (web/ratelimit.py): a burst
any human fits inside, a sustained rate a loop doesn't, keyed by the
client's address, and never for a read."""

from meshradio.web import ratelimit
from meshradio.web.ratelimit import Buckets

from .helpers import client_for, embed_app, page_app, peer, relay_embed_app


def test_buckets_refill_at_the_rate_and_cap_at_the_burst():
    b = Buckets(3, 1.0)
    assert [b.take("k", 0.0) for _ in range(4)] == [True, True, True, False]
    assert b.take("k", 0.5) is False                  # half a token back: not yet
    assert b.take("k", 1.0) is True                   # a whole one after a second
    assert b.take("other", 1.0) is True               # another key, its own bucket
    assert all(b.take("k", 100.0) for _ in range(3))  # full again after a long rest
    assert not b.take("k", 100.0)                     # and never more than the burst


def test_idle_clients_are_swept():
    b = Buckets(2, 1.0)
    for i in range(5):
        assert b.take(f"client-{i}", 0.0)
    b._sweep(1.0)
    assert b.tracked() == 5                           # a second idle: still remembered
    b._sweep(3.0)
    assert b.tracked() == 0                           # full again, so forgettable


async def test_presses_and_searches_are_limited_per_client(db, bus, monkeypatch):
    monkeypatch.setattr(ratelimit, "PRESSES", (5, 0.0))     # no refill: deterministic
    monkeypatch.setattr(ratelimit, "SEARCHES", (3, 0.0))
    app = embed_app(db, bus)
    async with client_for(app) as client:
        codes = [(await client.post("/api/volume/50")).status_code for _ in range(7)]
        assert codes == [200] * 5 + [429, 429]
        refused = await client.post("/api/volume/50")
        assert refused.headers["retry-after"] == "1"
        for _ in range(20):                                  # reads are never limited
            assert (await client.get("/api/state")).status_code == 200
        codes = [(await client.get("/search", params={"q": "rain"})).status_code
                 for _ in range(5)]
        assert codes == [200] * 3 + [429, 429]
    async with peer(app, "10.0.0.2") as other:               # another address, its own budget
        await other.get("/")
        assert (await other.post("/api/volume/50")).status_code == 200


async def test_the_relay_push_and_refused_cross_site_presses_cost_nothing(db, bus, monkeypatch):
    monkeypatch.setattr(ratelimit, "PRESSES", (2, 0.0))
    app = relay_embed_app(db, bus)
    async with client_for(app) as client:
        for _ in range(5):                                   # authenticated, its own caps
            resp = await client.post("/api/ingest", headers={"Authorization": "Bearer s3cret"},
                                     json={"messages": []})
            assert resp.status_code == 200
        for _ in range(5):                                   # the origin guard answers first
            resp = await client.post("/api/pause", headers={"origin": "http://evil.example"})
            assert resp.status_code == 403
        assert (await client.post("/api/pause")).status_code == 200   # budget untouched
        assert (await client.post("/api/pause")).status_code == 200
        assert (await client.post("/api/pause")).status_code == 429


async def test_the_limiter_can_be_switched_off(db, bus, monkeypatch):
    monkeypatch.setattr(ratelimit, "PRESSES", (1, 0.0))
    app = page_app(db, bus, rate_limit=False)
    async with client_for(app) as client:
        for _ in range(5):
            assert (await client.post("/api/volume/50")).status_code == 200
