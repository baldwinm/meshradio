"""The visitor's address behind a proxy that appends to X-Forwarded-For.

Render's proxy adds to whatever X-Forwarded-For a visitor sends instead of
replacing it. Read from the left, that header is the visitor's to write, and
every per-address limit is one forged header away from a fresh start. These
check that, with ``[web] proxy_hops`` set, only the entries the proxies
added count."""

import httpx
import pytest

from meshradio.config import Config, ConfigError, validate_config
from meshradio.web.proxy import forwarded_for, visitor_address

from .helpers import admin_settings, embed_app, peer, sign_in

# What Render's two hops add after whatever the visitor sent.
VISITOR = "198.51.100.7"
EDGE = "10.1.2.3"


def render_chain(forged: str = "") -> dict:
    chain = f"{forged}, {VISITOR}, {EDGE}" if forged else f"{VISITOR}, {EDGE}"
    return {"x-forwarded-for": chain, "x-forwarded-proto": "https"}


def test_the_visitor_is_counted_from_the_right():
    assert visitor_address([VISITOR, EDGE], 2) == VISITOR
    assert visitor_address(["6.6.6.6", "7.7.7.7", VISITOR, EDGE], 2) == VISITOR
    # Shorter than the hops: nobody padded it, so the leftmost is the first hop's.
    assert visitor_address([VISITOR], 2) == VISITOR
    assert visitor_address([], 2) is None


def test_repeated_header_lines_read_as_one_list():
    headers = [(b"x-forwarded-for", b"6.6.6.6"), (b"host", b"x"),
               (b"x-forwarded-for", b" 198.51.100.7 , 10.1.2.3")]
    assert forwarded_for(headers) == ["6.6.6.6", VISITOR, EDGE]


def test_proxy_hops_cant_be_negative():
    config = Config()
    config.web.proxy_hops = -1
    with pytest.raises(ConfigError, match="proxy_hops"):
        validate_config(config)


def _app(db, bus, tmp_path, hops=2, trusted=("*",)):
    return embed_app(db, bus, trusted_proxies=list(trusted), proxy_hops=hops,
                     admin=admin_settings(tmp_path))


async def test_a_forged_address_doesnt_reset_the_sign_in_lockout(db, bus, tmp_path):
    async with peer(_app(db, bus, tmp_path), "10.9.9.9") as client:
        for i in range(5):
            client.headers.update(render_chain(forged=f"203.0.113.{i}"))
            assert (await sign_in(client, password="nope")).status_code == 401
        client.headers.update(render_chain(forged="203.0.113.99"))
        assert (await sign_in(client)).status_code == 429
    entries = await db.admin_log_entries("sign-ins")
    assert {e["ip"] for e in entries} == {VISITOR}


async def test_without_hops_the_leftmost_entry_is_still_believed(db, bus, tmp_path):
    """The old reading, kept for proxies that overwrite the header: the
    app-level middleware stays out of the way and the peer is the client."""
    async with peer(_app(db, bus, tmp_path, hops=0), "10.9.9.9") as client:
        client.headers.update(render_chain(forged="203.0.113.1"))
        await sign_in(client, password="nope")
    assert [e["ip"] for e in await db.admin_log_entries("sign-ins")] == ["10.9.9.9"]


async def test_an_untrusted_peer_keeps_its_own_address(db, bus, tmp_path):
    async with peer(_app(db, bus, tmp_path, trusted=["127.0.0.1"]), "192.168.1.7") as client:
        client.headers.update(render_chain(forged="203.0.113.1"))
        resp = await sign_in(client, password="nope")
        assert "secure" not in resp.headers.get("set-cookie", "").lower()
    assert [e["ip"] for e in await db.admin_log_entries("sign-ins")] == ["192.168.1.7"]


async def test_over_https_the_admin_cookie_is_secure_and_the_page_says_so(db, bus, tmp_path):
    # An https URL, as the browser has it, so the client sends a Secure cookie back.
    transport = httpx.ASGITransport(app=_app(db, bus, tmp_path), client=("10.9.9.9", 1))
    async with httpx.AsyncClient(transport=transport, base_url="https://test") as client:
        client.headers.update(render_chain())
        login = await client.get("/admin/login")
        assert "isn't https" not in login.text
        resp = await sign_in(client)
        cookie = resp.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        page = (await client.get("/admin")).text
        assert "This connection" in page and VISITOR in page and "via 10.9.9.9" in page


async def test_plain_http_sign_in_warns_before_the_password_is_typed(db, bus, tmp_path):
    async with peer(_app(db, bus, tmp_path, hops=0), "192.168.1.7") as client:
        assert "isn't https" in (await client.get("/admin/login")).text
