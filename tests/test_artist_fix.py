"""Songs filed under a stand-in artist ("Release - Topic"): kept out at write
time, and fixable by hand from the admin page, many songs at once."""

from meshradio.media.cacher import ytdlp_artist

from .helpers import (
    admin_post,
    admin_settings,
    client_for,
    page_app,
    seed_shares,
    share,
    sign_in,
)

DAY = "2026-07-06"
A, B, C = "dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk"


async def _stored_as(db, track_id, artist):
    """Plant an artist as rows stored before the fix carry it, past the
    write-time rule that would now drop it."""
    async with db.transaction():
        await db.db.execute("UPDATE tracks SET artist=? WHERE id=?", (artist, track_id))


def _app(db, bus, tmp_path):
    return page_app(db, bus, admin=admin_settings(tmp_path))


async def test_a_stand_in_channel_name_is_stored_as_no_artist(db, bus):
    song = await share(db, DAY, A, "alice", title="Holocene", artist="Release - Topic")
    assert song["artist"] is None
    # A real artist still lands, and a later stand-in doesn't wipe it.
    await db.update_track_metadata(song["id"], artist="Bon Iver")
    await db.update_track_metadata(song["id"], artist="release - topic")
    assert (await db.track_by_id(song["id"]))["artist"] == "Bon Iver"
    assert all(r["artist"] != "Release" for r in await db.top_artists())


def test_ytdlp_credits_outrank_the_uploading_channel():
    assert ytdlp_artist({"artist": "Bon Iver", "uploader": "Release - Topic"}) == "Bon Iver"
    assert ytdlp_artist({"artists": ["Simon", "Garfunkel"], "uploader": "x"}) == \
        "Simon, Garfunkel"
    assert ytdlp_artist({"uploader": "Release - Topic"}) == "Release - Topic"
    assert ytdlp_artist({}) is None


async def test_the_fix_screen_lists_stand_in_and_missing_artists(db, bus, tmp_path):
    bad = await share(db, DAY, A, "alice", title="Holocene")
    await _stored_as(db, bad["id"], "Release - Topic")
    await share(db, DAY, B, "bob", title="Gangnam Style")             # no artist
    await share(db, DAY, C, "carol", title="Despacito", artist="Luis Fonsi")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        page = await client.get("/admin/artists")
        assert "2 songs" in page.text and "/admin/artists/fix" in page.text
        listed = await client.get("/admin/artists/fix")
        assert "Holocene" in listed.text and "Gangnam Style" in listed.text
        assert "Despacito" not in listed.text
        # The name the public pages show works too: "Release", no suffix.
        named = await client.get("/admin/artists/fix", params={"name": "release"})
        assert "Holocene" in named.text and "Gangnam Style" not in named.text


async def test_saving_sets_every_share_and_one_undo_puts_them_back(db, bus, tmp_path):
    shares = await seed_shares(db, A, ["2026-07-01", "2026-07-02"], title="Holocene")
    for t in shares:
        await _stored_as(db, t["id"], "Release - Topic")
    other = await share(db, DAY, B, "bob", title="Gangnam Style")
    untouched = await share(db, DAY, C, "carol", title="Despacito")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, "/admin/artists/fix", name="Release", **{
            f"artist.{A}": "Bon Iver", f"artist.{B}": " PSY ", f"artist.{C}": "",
            "artist.../etc": "nope",
        })
        assert resp.status_code == 303 and "name=Release" in resp.headers["location"]
        for t in shares:
            row = await db.track_by_id(t["id"])
            assert row["artist"] == "Bon Iver" and row["meta_edited_at"]
        assert (await db.track_by_id(other["id"]))["artist"] == "PSY"
        assert (await db.track_by_id(untouched["id"]))["artist"] is None
        # A late oEmbed answer can't put the stand-in back.
        await db.update_track_metadata(shares[0]["id"], artist="Someone Else")
        assert (await db.track_by_id(shares[0]["id"]))["artist"] == "Bon Iver"

        [entry] = await db.admin_log_entries("changes")
        assert entry["action"] == "fix_artists" and entry["before"] == "3 songs"
        await admin_post(client, f"/admin/log/{entry['id']}/undo")
    for t in shares:
        row = await db.track_by_id(t["id"])
        assert row["artist"] == "Release - Topic" and row["meta_edited_at"] is None
    assert (await db.track_by_id(other["id"]))["artist"] is None


async def test_a_blank_form_changes_nothing(db, bus, tmp_path):
    await share(db, DAY, A, "alice", title="Holocene")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        resp = await admin_post(client, "/admin/artists/fix", **{f"artist.{A}": "  "})
        assert "at least one song" in (await client.get(resp.headers["location"])).text
    assert await db.admin_log_entries("changes") == []


async def test_a_fixed_song_shared_again_keeps_the_fix(db, bus, tmp_path):
    first = await share(db, "2026-07-01", A, "alice", title="Holocene")
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        await sign_in(client)
        await admin_post(client, "/admin/artists/fix", **{f"artist.{A}": "Bon Iver"})
    assert (await db.track_by_id(first["id"]))["artist"] == "Bon Iver"
    # Shared on another day: oEmbed answers with the stand-in again.
    again = await share(db, "2026-07-09", A, "bob", title="Holocene",
                        artist="Release - Topic")
    assert again["artist"] == "Bon Iver" and again["meta_edited_at"]


async def test_the_fix_screen_needs_a_sign_in(db, bus, tmp_path):
    async with client_for(_app(db, bus, tmp_path), visited=False) as client:
        page = await client.get("/admin/artists/fix")
        assert page.status_code in (303, 307) and "/admin/login" in page.headers["location"]
        resp = await client.post("/admin/artists/fix", data={f"artist.{A}": "x"})
        assert resp.status_code in (303, 307, 403)
