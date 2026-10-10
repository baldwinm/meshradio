"""Seeing whether the Pi's auto-updater works: its report (written by
deploy/auto-update.sh, see test_auto_update.py) read on the Pi, relayed to
the hosted site, and shown on the admin overview and in /healthz."""

import time

import httpx

from meshradio import __version__, deployinfo
from meshradio.config import RelayConfig
from meshradio.ingest.relay import RelayPusher

from .helpers import (
    admin_settings,
    client_for,
    page_app,
    relay_embed_app,
    sign_in,
    write_autoupdate_report,
)

SHA = "0123456789abcdef0123456789abcdef01234567"
AUTH = {"Authorization": "Bearer s3cret"}


def test_a_report_is_checked_field_by_field():
    now = time.time()
    good = deployinfo.clean_status({"checked_at": now, "result": "updated", "message": "x" * 999,
                                    "commit": SHA, "target": "<script>", "updated_at": "soon"})
    assert good["result"] == "updated" and good["commit"] == SHA
    assert good["target"] is None and good["updated_at"] is None
    assert len(good["message"]) == 300
    for bad in (None, [], {"result": "updated"}, {"result": "pwned", "checked_at": now},
                {"result": "current", "checked_at": True}):
        assert deployinfo.clean_status(bad) is None
    assert deployinfo.clean_node({"version": "1.0; rm -rf", "commit": SHA})["version"] is None


def test_a_quiet_updater_reads_as_broken_after_half_an_hour():
    now = time.time()
    fresh = deployinfo.clean_status({"checked_at": now - 60, "result": "current"})
    assert deployinfo.describe(fresh, now)["state"] == "ok"
    stale = {**fresh, "checked_at": now - deployinfo.STALE_S - 1}
    view = deployinfo.describe(stale, now)
    assert view["state"] == "bad" and "timer" in view["text"]
    assert deployinfo.describe({**fresh, "result": "blocked"}, now)["ok"] is False
    assert deployinfo.describe(None, now)["state"] == "none"


def test_the_running_commit_is_read_from_the_clone(tmp_path):
    git = tmp_path / ".git"
    (git / "refs" / "heads").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    (git / "packed-refs").write_text(f"# pack-refs\n{SHA} refs/heads/main\n")
    assert deployinfo._git_head(tmp_path) == SHA          # packed (a fresh clone)
    (git / "refs" / "heads" / "main").write_text("f" * 40 + "\n")
    assert deployinfo._git_head(tmp_path) == "f" * 40     # loose wins
    (git / "HEAD").write_text(SHA + "\n")
    assert deployinfo._git_head(tmp_path) == SHA          # detached
    assert deployinfo._git_head(tmp_path / "nowhere") is None


async def test_healthz_on_the_pi_carries_its_own_report(db, bus):
    async with client_for(page_app(db, bus)) as client:
        body = (await client.get("/healthz")).json()
        assert body["version"] == __version__
        assert body["pi_update"] is None
        write_autoupdate_report(result="blocked", message="local edits to x are in the way")
        body = (await client.get("/healthz")).json()
    assert body["pi_update"]["ok"] is False
    assert body["pi_update"]["result"] == "blocked"
    assert body["pi_update"]["commit"] == "aaaaaaa"
    assert "message" not in body["pi_update"]      # public: no detail


async def test_the_relay_sends_the_pis_state_along(db, bus):
    write_autoupdate_report(result="updated")
    pusher = RelayPusher(RelayConfig(push_url="https://radio.example.org/", token="s3cret"), db)
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(httpx.Response(200, content=request.content).json())
        return httpx.Response(200, json={"ok": True, "inserted": 0})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await pusher.push_once(client)
    assert sent["node"]["version"] == __version__
    assert sent["node"]["autoupdate"]["result"] == "updated"


async def test_the_hosted_overview_shows_what_the_pi_relayed(db, bus, tmp_path):
    app = relay_embed_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        before = (await client.get("/admin")).text
        assert "No word from the Pi" in before
        assert (await client.get("/healthz")).json()["pi_update"] is None
        node = {"version": "0.9.0", "commit": SHA, "autoupdate": {
            "checked_at": time.time() - 120, "result": "rolled-back", "commit": SHA,
            "target": "b" * 40, "message": "bbbbbbb didn't come up healthy", "updated_at": None}}
        resp = await client.post("/api/ingest", json={"messages": [], "node": node}, headers=AUTH)
        assert resp.status_code == 200
        page = (await client.get("/admin")).text
        assert "The Pi runs <strong>v0.9.0</strong>" in page and SHA[:7] in page
        assert "not the same as this site" in page or deployinfo.running_commit() is None
        assert "Pi auto-update: The new commit didn&#39;t come up healthy" in page   # Needs a look
        assert "bbbbbbb didn&#39;t come up healthy" in page
        health = (await client.get("/healthz")).json()["pi_update"]
        assert health["ok"] is False and health["result"] == "rolled-back"
        # A push without the field (an older Pi) or with junk keeps what was known.
        await client.post("/api/ingest", json={"messages": [], "node": "junk"}, headers=AUTH)
        assert "v0.9.0" in (await client.get("/admin")).text


async def test_the_pi_overview_shows_its_own_updater(db, bus, tmp_path):
    app = page_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        page = (await client.get("/admin")).text
        assert "No report from the auto-updater on this machine" in page
        write_autoupdate_report(result="current", updated_at=time.time() - 7200)
        page = (await client.get("/admin")).text
        assert "Up to date" in page and "last updated 2 h ago" in page
        assert "Pi auto-update:" not in page          # nothing for Needs a look
        write_autoupdate_report(checked_at=time.time() - 3 * 3600)
        page = (await client.get("/admin")).text
        assert "Hasn&#39;t checked for updates lately" in page
        assert "Pi auto-update: Hasn&#39;t checked" in page
        assert "journalctl -u meshradio-autoupdate" in page
