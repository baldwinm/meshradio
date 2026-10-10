"""Seeing whether the Pi's auto-updater works: its report (written by
deploy/auto-update.sh, see test_auto_update.py) read on the Pi, relayed to
the hosted site, and shown on the admin overview and in /healthz."""

import json
import time

import httpx
import pytest

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
SITE = "c" * 40
AUTH = {"Authorization": "Bearer s3cret"}


@pytest.fixture
def site_commit(monkeypatch):
    """Pin the commit this process reports as running (it's cached, and would
    otherwise be whatever the test checkout has)."""
    monkeypatch.setenv("MESHRADIO_COMMIT", SITE)
    deployinfo.running_commit.cache_clear()
    yield SITE
    deployinfo.running_commit.cache_clear()


def _node(commit, result="current", clone=None, autoupdate=True):
    report = {"checked_at": time.time() - 60, "result": result, "commit": clone or commit,
              "target": clone or commit, "message": "up to date", "updated_at": None}
    return {"version": "0.11.1", "commit": commit, "autoupdate": report if autoupdate else None}


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


async def test_the_hosted_overview_shows_what_the_pi_relayed(db, bus, tmp_path, site_commit):
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
        assert "its auto-updater isn't moving it" in page
        assert "Normal right after a merge" not in page
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


def test_the_mismatch_clock_restarts_only_when_the_pi_moves():
    now = time.time()
    first = deployinfo.track_mismatch(None, _node(SHA), SITE, now)
    assert first == {"pi": SHA, "since": now}
    # Still on the same commit, whatever the site did meanwhile: keeps running.
    assert deployinfo.track_mismatch(first, _node(SHA), "e" * 40, now + 600) == first
    # The Pi moved (it is updating), just not onto this site's commit yet.
    moved = deployinfo.track_mismatch(first, _node("d" * 40), SITE, now + 600)
    assert moved == {"pi": "d" * 40, "since": now + 600}
    assert deployinfo.track_mismatch(first, _node(SITE), SITE, now) is None
    assert deployinfo.track_mismatch(None, _node(SITE[:7]), SITE, now) is None   # short form
    assert deployinfo.track_mismatch(None, _node(SHA), None, now) is None        # site unknown
    # A docs-only update moves the clone without a restart: the clone counts...
    assert deployinfo.track_mismatch(None, _node(SHA, clone=SITE), SITE, now) is None
    # ...while its report is fresh. One left behind by a stopped timer says
    # nothing about a later update by hand.
    stale = _node(SITE, clone=SHA)
    stale["autoupdate"]["checked_at"] = now - 2 * 3600
    assert deployinfo.track_mismatch(None, stale, SITE, now) is None


async def _push(client, node):
    resp = await client.post("/api/ingest", json={"messages": [], "node": node}, headers=AUTH)
    assert resp.status_code == 200


async def _backdate_mismatch(db, seconds):
    """Pretend the Pi has been on its commit ``seconds`` longer."""
    from meshradio.web.routes_ingest import MISMATCH_KEY
    record = json.loads(await db.get_setting(MISMATCH_KEY))
    record["since"] -= seconds
    await db.set_setting(MISMATCH_KEY, json.dumps(record))


async def test_a_pi_catching_up_is_normal_until_it_falls_out_of_step(
        db, bus, tmp_path, site_commit):
    app = relay_embed_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        # Just after a merge: the site has deployed, the Pi hasn't checked yet.
        await _push(client, _node(SHA))
        page = (await client.get("/admin")).text
        assert "Normal right after a merge" in page and "every ten minutes" in page
        assert "Out of step" not in page and "different commit from this site for 0" in page
        view = (await client.get("/healthz")).json()["pi_update"]
        assert view["ok"] is True and view["site_mismatch_s"] < 60

        # Still on it half an hour on, while its updater keeps saying "current"
        # (the deploy branch stopped moving, say): out of step, and an alert.
        await _backdate_mismatch(db, deployinfo.LAG_S + 60)
        await _push(client, _node(SHA))
        page = (await client.get("/admin")).text
        assert "Out of step" in page
        assert "The Pi has been on a different commit from this site for 31 min" in page
        view = (await client.get("/healthz")).json()["pi_update"]
        assert view["ok"] is False and view["result"] == "current"

    # The site redeploys (a new process, nothing in memory): the Pi is still
    # stuck, and the clock says so rather than starting again.
    app = relay_embed_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        await _push(client, _node(SHA))
        view = (await client.get("/healthz")).json()["pi_update"]
        assert view["ok"] is False and view["site_mismatch_s"] > deployinfo.LAG_S

        # Caught up: the clock stops and the warning goes.
        await _push(client, _node(SITE))
        page = (await client.get("/admin")).text
        assert "Same commit as this site." in page and "Out of step" not in page
        view = (await client.get("/healthz")).json()["pi_update"]
        assert view["ok"] is True and view["site_mismatch_s"] is None


async def test_a_pi_without_the_updater_is_not_expected_to_keep_up(
        db, bus, tmp_path, site_commit):
    app = relay_embed_app(db, bus, admin=admin_settings(tmp_path))
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        await _push(client, _node(SHA, autoupdate=False))
        await _backdate_mismatch(db, 10 * deployinfo.LAG_S)
        await _push(client, _node(SHA, autoupdate=False))
        page = (await client.get("/admin")).text
        assert "On a different commit from this site.</p>" in page
        assert "Out of step" not in page and "Normal right after a merge" not in page
        assert (await client.get("/healthz")).json()["pi_update"] is None
