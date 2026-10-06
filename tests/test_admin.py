"""The admin page: signing in, the activity log, and every fix it makes.

What matters here is that the page can't be reached without the password,
that each change does exactly what the matching CLI flag does, that each is
logged, and that the undoable ones really undo."""

import asyncio
import base64
import json
import sqlite3
import time
from argparse import Namespace

import pytest

from meshradio import backup as backup_mod
from meshradio.bus import INGEST_STATUS
from meshradio.cli import _run_set_theme
from meshradio.config import Config, ConfigError, load_config
from meshradio.db import Database
from meshradio.ingest.corescope import FeedProbe
from meshradio.web import routes_admin
from meshradio.web.admin_auth import (
    LoginThrottle,
    check_totp,
    hash_password,
    new_totp_secret,
    totp_at,
    verify_password,
)
from meshradio.web.routes_admin import artist_key, lookalike_groups

from .helpers import (
    ADMIN_PASSWORD,
    admin_post,
    admin_settings,
    client_for,
    embed_app,
    page_app,
    peer,
    sign_in,
)

DAY = "2026-07-06"
VID = "dQw4w9WgXcQ"
OTHER = "9bZkp7q19f0"


async def _song(db, video_id=VID, date=DAY, title="Never Gonna", artist=None, theme=None):
    theme = theme or await db.latest_theme_for_date(date) or await db.create_theme(date, "water")
    track = await db.add_track(
        video_id=video_id, url=f"https://youtu.be/{video_id}", channel="#music",
        sender="alice", mesh_ts=time.time(), source="mesh", theme_id=theme["id"],
    )
    await db.update_track_metadata(track["id"], title=title, artist=artist)
    return await db.track_by_id(track["id"])


def _app(db, bus, tmp_path, **kwargs):
    return page_app(db, bus, admin=admin_settings(tmp_path, **kwargs))


# -- password, codes, lockout -------------------------------------------------


def test_a_password_hash_checks_only_its_own_password():
    stored = hash_password("hunter2 hunter2", n=16)
    assert verify_password("hunter2 hunter2", stored)
    assert not verify_password("hunter2 hunter3", stored)
    assert not verify_password("hunter2 hunter2", "plaintext")
    # A hand-edited cost can't ask scrypt for gigabytes.
    assert not verify_password("x", stored.replace("$16$", "$1073741824$", 1))


def test_authenticator_codes_follow_rfc_6238_and_refuse_a_replay():
    # RFC 6238's SHA-1 test key, built here so no scanner reads it as a secret
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert totp_at(secret, 59 // 30) == "287082"         # its T=59 vector, 6 digits
    counter = check_totp(secret, "287082", 59)
    assert counter == 1
    assert check_totp(secret, "287082", 59, last_counter=counter) is None
    assert check_totp(secret, "287082", 59 + 30 * 5) is None   # long expired
    assert check_totp(new_totp_secret(), "287082", 59) is None


def test_five_failures_pause_one_address_for_the_window():
    throttle = LoginThrottle(max_failures=5, window_s=900)
    for i in range(5):
        assert throttle.locked_for("1.2.3.4", 1000 + i) == 0
        throttle.fail("1.2.3.4", 1000 + i)
    assert throttle.locked_for("1.2.3.4", 1010) > 0
    assert throttle.locked_for("5.6.7.8", 1010) == 0
    assert throttle.locked_for("1.2.3.4", 1000 + 901) == 0


# -- getting in ---------------------------------------------------------------


async def test_without_a_password_hash_there_is_no_admin_page(db, bus):
    async with client_for(page_app(db, bus), visited=False) as client:
        assert (await client.get("/admin")).status_code == 404
        assert (await client.get("/admin/login")).status_code == 404


async def test_a_page_sends_a_stranger_to_sign_in_and_back(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        resp = await client.get("/admin/days?untitled=1")
        assert resp.status_code == 303
        assert resp.headers["location"] == "/admin/login?next=%2Fadmin%2Fdays%3Funtitled%3D1"
        resp = await client.post(
            "/admin/login", data={"password": ADMIN_PASSWORD, "next": "/admin/days?untitled=1"}
        )
        assert resp.headers["location"] == "/admin/days?untitled=1"


async def test_signing_in_sets_a_strict_admin_only_cookie(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        resp = await sign_in(client)
        assert resp.status_code == 303 and resp.headers["location"] == "/admin"
        cookie = resp.headers["set-cookie"].lower()
        assert "mr_admin=" in cookie
        assert "path=/admin" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        page = await client.get("/admin")
        assert page.status_code == 200
        assert page.headers["cache-control"] == "no-store"
        assert "noindex" in page.headers["x-robots-tag"]
        assert "mr_sid" not in client.cookies        # no visitor session minted
    assert [e["action"] for e in await db.admin_log_entries()] == ["sign_in"]


async def test_a_wrong_password_is_logged_and_five_lock_the_address(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        for _ in range(5):
            assert (await sign_in(client, password="nope")).status_code == 401
        locked = await sign_in(client)               # even the right one, for now
        assert locked.status_code == 429
        assert "paused" in locked.text
    actions = [e["action"] for e in await db.admin_log_entries("sign-ins")]
    assert actions == ["sign_in_failed"] * 5


async def test_after_signing_in_only_admin_pages_are_landing_spots(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        for target in ("//evil.example/admin", "https://evil.example", "/archive"):
            resp = await client.post(
                "/admin/login", data={"password": ADMIN_PASSWORD, "next": target}
            )
            assert resp.headers["location"] == "/admin"


async def test_a_new_password_signs_every_browser_out(db, bus, tmp_path):
    app = _app(db, bus, tmp_path)
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        app.state.admin.password_hash = hash_password("a different one", n=16)
        assert (await client.get("/admin")).status_code == 303


async def test_an_idle_sign_in_expires(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        await db.db.execute("UPDATE admin_sessions SET seen_at = seen_at - 3600")
        assert (await client.get("/admin")).status_code == 303
        assert await db._fetchone("SELECT 1 FROM admin_sessions") is None


async def test_a_form_without_its_csrf_token_is_refused(db, bus, tmp_path):
    await _song(db)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await client.post(f"/admin/days/{DAY}/theme", data={"title": "fire"})
        assert resp.status_code == 403
        resp = await client.post(
            f"/admin/days/{DAY}/theme", data={"title": "fire", "csrf": "0" * 64}
        )
        assert resp.status_code == 403
    assert (await db.latest_theme_for_date(DAY))["title"] == "water"


async def test_two_step_sign_in_needs_a_fresh_code(db, bus, tmp_path):
    secret = new_totp_secret()
    app = _app(db, bus, tmp_path, totp_secret=secret)
    code = totp_at(secret, int(time.time() // 30))
    async with client_for(app, visited=False) as client:
        assert "Authenticator code" in (await client.get("/admin/login")).text
        assert (await sign_in(client)).status_code == 401                # no code
        assert (await sign_in(client, code="000000" if code != "000000" else "111111")
                ).status_code == 401
        assert (await sign_in(client, code=code)).status_code == 303
    async with client_for(app, visited=False) as other:
        assert (await sign_in(other, code=code)).status_code == 401      # replayed


async def test_robots_keeps_crawlers_off_admin(db, bus):
    async with client_for(page_app(db, bus), visited=False) as client:
        assert "Disallow: /admin" in (await client.get("/robots.txt")).text


# -- days and themes ----------------------------------------------------------


async def test_renaming_a_theme_is_logged_and_undoable(db, bus, tmp_path):
    await _song(db)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, f"/admin/days/{DAY}/theme", title="songs about rain")
        assert resp.status_code == 303
        theme = await db.latest_theme_for_date(DAY)
        assert theme["title"] == "songs about rain" and theme["locked"]
        [entry] = await db.admin_log_entries("changes")
        assert (entry["before"], entry["after"]) == ("water", "songs about rain")
        day = await client.get(resp.headers["location"])
        assert "Undo" in day.text and "songs about rain" in day.text

        await admin_post(client, f"/admin/log/{entry['id']}/undo")
        assert (await db.latest_theme_for_date(DAY))["title"] == "water"
        assert (await db.admin_log_entry(entry["id"]))["undone_by"]
        again = await admin_post(client, f"/admin/log/{entry['id']}/undo")
        assert "already" in (await client.get(again.headers["location"])).text


async def test_the_untitled_filter_lists_placeholder_days_with_songs(db, bus, tmp_path):
    placeholder = await db.create_theme("2026-07-07", "Untitled — 2026-07-07")
    await _song(db, theme=placeholder)
    await _song(db, video_id=OTHER)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        page = (await client.get("/admin/days?untitled=1")).text
        assert "2026-07-07" in page and DAY not in page
        overview = (await client.get("/admin")).text
        assert "1 day still untitled: 2026-07-07" in overview


async def test_a_day_that_was_never_archived_is_a_404(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        assert (await client.get("/admin/days/2026-01-01")).status_code == 404
        assert (await client.get("/admin/days/not-a-date")).status_code == 404


# -- removing and putting back ------------------------------------------------


async def test_removing_a_song_needs_the_typed_date(db, bus, tmp_path):
    track = await _song(db)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        confirm = await client.get(f"/admin/tracks/{track['id']}/remove")
        assert f'data-confirm-value="{DAY}"' in confirm.text
        resp = await admin_post(client, f"/admin/tracks/{track['id']}/remove",
                                confirm="2026-07-07")
        assert resp.status_code == 400
    assert await db.track_by_id(track["id"]) is not None


async def test_a_removal_backs_up_first_and_undo_puts_the_song_back(db, bus, tmp_path):
    track = await _song(db, artist="Rick Astley")
    keep = await _song(db, video_id=OTHER)          # so the day itself stays
    settings = admin_settings(tmp_path)
    settings.config.data_dir = tmp_path
    db_file = tmp_path / "meshradio.db"
    sqlite3.connect(db_file).close()                 # something to snapshot
    async with client_for(page_app(db, bus, admin=settings), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, f"/admin/tracks/{track['id']}/remove", confirm=DAY)
        assert resp.status_code == 303
        assert await db.track_by_id(track["id"]) is None
        assert await db.is_deleted(keep["theme_id"], VID)
        names = [p.name for p in backup_mod.list_snapshots(settings.config.backup_dir)]
        assert any(n.endswith("-preremove.db") for n in names)
        [entry] = await db.admin_log_entries("changes")
        assert entry["action"] == "remove_track"

        await admin_post(client, f"/admin/log/{entry['id']}/undo")
    back = [t for t in await db.tracks_for_day(DAY) if t["video_id"] == VID]
    assert len(back) == 1
    assert (back[0]["title"], back[0]["artist"]) == ("Never Gonna", "Rick Astley")
    assert not await db.is_deleted(keep["theme_id"], VID)


async def test_a_cli_removal_without_details_only_lifts_the_block(db, bus, tmp_path):
    await _song(db, video_id=OTHER)
    await db.db.execute(
        "INSERT INTO deleted_tracks(date,video_id,title,sender,deleted_at) "
        "VALUES(?,?,?,?,?)", (DAY, VID, "old one", "bob", "2026-07-06T12:00:00Z"),
    )
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        page = await client.get("/admin/removed")
        assert "Allow back" in page.text
        await admin_post(client, f"/admin/removed/{DAY}/{VID}/restore")
    assert await db.removed_tracks() == []
    assert [t["video_id"] for t in await db.tracks_for_day(DAY)] == [OTHER]


async def test_removing_a_days_only_song_drops_its_placeholder(db, bus, tmp_path):
    placeholder = await db.create_theme("2026-07-07", "Untitled — 2026-07-07")
    track = await _song(db, theme=placeholder)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, f"/admin/tracks/{track['id']}/remove",
                                confirm="2026-07-07")
        assert resp.headers["location"].startswith("/admin/removed")
    assert await db.latest_theme_for_date("2026-07-07") is None
    # Putting it back makes the day again.
    assert await db.restore_deleted_track("2026-07-07", VID) is not None
    assert await db.latest_theme_for_date("2026-07-07") is not None


# -- song details -------------------------------------------------------------


async def test_an_edited_song_keeps_its_details_until_undone(db, bus, tmp_path):
    track = await _song(db, title="Never Gonna (Official Video) [HD]", artist="RickAstleyVEVO")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        await admin_post(client, f"/admin/tracks/{track['id']}/edit",
                         title="Never Gonna Give You Up", artist="Rick Astley")
        # A late oEmbed answer or relay re-push can't put the old text back.
        await db.update_track_metadata(track["id"], title="junk", artist="junk", duration=200)
        edited = await db.track_by_id(track["id"])
        assert (edited["title"], edited["artist"]) == ("Never Gonna Give You Up", "Rick Astley")
        assert edited["duration"] == 200
        [entry] = await db.admin_log_entries("changes")
        await admin_post(client, f"/admin/log/{entry['id']}/undo")
    reverted = await db.track_by_id(track["id"])
    assert reverted["title"] == "Never Gonna (Official Video) [HD]"
    assert reverted["meta_edited_at"] is None


async def test_an_edit_needs_a_title(db, bus, tmp_path):
    track = await _song(db)
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, f"/admin/tracks/{track['id']}/edit", title=" ")
        assert resp.status_code == 400
    assert (await db.track_by_id(track["id"]))["title"] == "Never Gonna"


# -- artists ------------------------------------------------------------------


def test_lookalike_spellings_group_but_different_names_dont():
    spellings = [
        {"artist": "The Beatles", "songs": 9},
        {"artist": "Beatles", "songs": 2},
        {"artist": "The Beatles - Topic", "songs": 12},
        {"artist": "Simon & Garfunkel", "songs": 3},
        {"artist": "Simon and Garfunkel", "songs": 1},
        {"artist": "Prince", "songs": 4},
        {"artist": "Prince & The Revolution", "songs": 2},
    ]
    groups = lookalike_groups(spellings, ignored=set())
    assert [g["suggested"] for g in groups] == ["The Beatles", "Simon & Garfunkel"]
    assert artist_key("The Beatles - Topic") == artist_key("beatles")
    assert lookalike_groups(spellings, ignored={artist_key("Beatles")})[0]["key"] == \
        artist_key("Simon and Garfunkel")


async def test_a_merge_respells_old_and_new_songs_and_undo_restores(db, bus, tmp_path):
    a = await _song(db, artist="Beatles")
    b = await _song(db, video_id=OTHER, artist="The Beatles - Topic")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        page = await client.get("/admin/artists")
        assert "Beatles" in page.text and "look-alike group" in page.text
        await admin_post(client, "/admin/artists/merge",
                         spelling=["Beatles", "The Beatles - Topic"], canonical="The Beatles")
        assert (await db.track_by_id(a["id"]))["artist"] == "The Beatles"
        assert (await db.track_by_id(b["id"]))["artist"] == "The Beatles"
        # A song arriving later with an old spelling joins the merged name.
        late = await _song(db, video_id="kJQP7kiw5Fk", artist="Beatles")
        assert late["artist"] == "The Beatles"

        [entry] = await db.admin_log_entries("changes")
        await admin_post(client, f"/admin/log/{entry['id']}/undo")
    assert (await db.track_by_id(a["id"]))["artist"] == "Beatles"
    assert (await db.track_by_id(b["id"]))["artist"] == "The Beatles - Topic"
    assert await db.artist_aliases() == []


async def test_a_hand_merge_must_name_a_spelling_some_song_has(db, bus, tmp_path):
    await _song(db, artist="Sinatra")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, "/admin/artists/merge", spelling="Sinatr",
                                canonical="Frank Sinatra")
        assert "No song carries that name" in (await client.get(resp.headers["location"])).text
        await admin_post(client, "/admin/artists/merge", spelling="Sinatra",
                         canonical="Frank Sinatra")
    assert (await db.artist_spellings())[0]["artist"] == "Frank Sinatra"


# -- feeds, backups, config, device -------------------------------------------


async def test_feeds_show_what_each_poller_last_reported(db, bus, tmp_path):
    app = _app(db, bus, tmp_path, comchan__enabled=True)
    async with app.router.lifespan_context(app), client_for(app, visited=False) as client:
        await sign_in(client)
        await asyncio.sleep(0.02)
        bus.publish(INGEST_STATUS, {"comchan": "error"})
        await asyncio.sleep(0.02)
        page = (await client.get("/admin/feeds")).text
        assert "No answer" in page
        assert "comchan (backup): No answer" in (await client.get("/admin")).text


async def test_a_probe_reports_without_touching_the_archive(db, bus, tmp_path, monkeypatch):
    async def fake_probe(client, channel, name="corescope", sample=5):
        return FeedProbe(name=name, channel=channel, error="timed out")

    monkeypatch.setattr(routes_admin, "probe", fake_probe)
    app = _app(db, bus, tmp_path, corescope__base_url="https://scope.example")
    async with client_for(app, visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, "/admin/feeds/corescope/probe")
        assert resp.status_code == 200
        assert "FAILED" in resp.text and "timed out" in resp.text
        assert (await admin_post(client, "/admin/feeds/nope/probe")).status_code == 404
    [entry] = await db.admin_log_entries("changes")
    assert (entry["action"], entry["after"]) == ("probe_feed", "failed")


async def test_backups_can_be_taken_and_only_listed_ones_downloaded(db, bus, tmp_path):
    settings = admin_settings(tmp_path)
    sqlite3.connect(tmp_path / "meshradio.db").close()
    async with client_for(page_app(db, bus, admin=settings), visited=False) as client:
        await sign_in(client)
        await admin_post(client, "/admin/backups")
        [snap] = backup_mod.list_snapshots(settings.config.backup_dir)
        assert snap.name.endswith("-manual.db")
        page = await client.get("/admin/backups")
        assert "Taken from this page" in page.text
        download = await client.get(f"/admin/backups/{snap.name}")
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]
        assert (await client.get("/admin/backups/..%2Fmeshradio.db")).status_code == 404
    actions = {e["action"] for e in await db.admin_log_entries("changes")}
    assert actions == {"backup", "download_backup"}


async def test_the_config_view_masks_secrets_and_hides_device_keys_in_public(
    db, bus, tmp_path
):
    settings = admin_settings(tmp_path, web__ingest_token="supersecret-1234")
    async with client_for(embed_app(db, bus, admin=settings), visited=False) as client:
        await sign_in(client)
        page = (await client.get("/admin/config")).text
        assert "•••• 1234" in page and "supersecret" not in page
        assert "quiet_hours" not in page and "mesh.serial_port" not in page
        assert settings.password_hash not in page
        assert (await client.get("/admin/device")).status_code == 404
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        assert "quiet_hours" in (await client.get("/admin/config")).text
        assert (await client.get("/admin/device")).status_code == 200


async def test_retrying_a_failed_download_sends_it_back_to_the_cacher(db, bus, tmp_path):
    track = await _song(db)
    await db.set_cache_status(track["id"], "failed")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        assert "Retry" in (await client.get("/admin/device")).text
        await admin_post(client, f"/admin/tracks/{track['id']}/retry")
    assert (await db.track_by_id(track["id"]))["cache_status"] == "pending"


async def test_the_admin_page_isnt_rate_limited_out_of_reach_by_others(db, bus, tmp_path):
    """Sign-in POSTs share the press budget, per address: someone else's
    flood doesn't lock the operator out."""
    app = _app(db, bus, tmp_path)
    async with peer(app, "9.9.9.9") as flood:
        for _ in range(40):
            await flood.post("/admin/login", data={"password": "x"})
    async with peer(app, "1.1.1.1") as me:
        assert (await sign_in(me)).status_code == 303


# -- the CLI writes to the same log -------------------------------------------


async def test_cli_fixes_land_in_the_activity_log(db, tmp_path):
    await db.create_theme(DAY, "water")
    await db.close()
    config = Config(data_dir=tmp_path)
    db.path.rename(config.db_path)
    assert await _run_set_theme(config, Namespace(set_theme="fire", theme_date=DAY)) == 0
    check = Database(config.db_path)
    await check.connect()
    try:
        [entry] = await check.admin_log_entries()
        assert (entry["actor"], entry["action"], entry["after"]) == ("cli", "rename_theme", "fire")
        assert json.loads(entry["undo"])["title"] == "water"
    finally:
        await check.close()
    await db.connect()


# -- config -------------------------------------------------------------------


def test_config_refuses_a_hash_nobody_could_sign_in_with(tmp_path, monkeypatch):
    path = tmp_path / "c.toml"
    path.write_text('[web]\nadmin_password_hash = "hunter2"\n')
    with pytest.raises(ConfigError, match="admin_password_hash"):
        load_config(path)
    path.write_text(f'[web]\nadmin_totp_secret = "{new_totp_secret()}"\n')
    with pytest.raises(ConfigError, match="needs a password"):
        load_config(path)
    good = hash_password("long enough password", n=16)
    path.write_text("")
    monkeypatch.setenv("MESHRADIO_ADMIN_PASSWORD_HASH", good)
    assert load_config(path).web.admin_password_hash == good
