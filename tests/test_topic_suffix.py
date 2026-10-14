"""YouTube's " - Topic" channel suffix: dropped where artists are stored, so
every page shows the artist's plain name, and cleaned from rows stored
before that by migration 15."""

from meshradio.db import MIGRATIONS, Database

from .helpers import client_for, page_app, share

DAY = "2026-07-06"
A, B, C, D = "dQw4w9WgXcQ", "9bZkp7q19f0", "kJQP7kiw5Fk", "aaaaaaaaaaa"


async def _plant(db, track_id, artist):
    """An artist as rows stored before the fix carry it."""
    async with db.transaction():
        await db.db.execute("UPDATE tracks SET artist=? WHERE id=?", (artist, track_id))


async def test_a_topic_channel_is_stored_under_the_plain_name(db, bus):
    song = await share(db, DAY, A, "alice", artist="Nilsson - Topic")
    assert song["artist"] == "Nilsson"
    assert await db.canonical_artist("  Bon Iver - TOPIC ") == "Bon Iver"
    # The stand-in channel is still no artist at all.
    assert await db.canonical_artist("Release - Topic") is None


async def test_merges_apply_to_the_topic_spelling_and_the_plain_one(db, bus):
    async with db.transaction():
        await db.db.execute(
            "INSERT INTO artist_aliases(alias,canonical,created_at) VALUES "
            "('Beatles - Topic','The Beatles','x'), ('Sinatra','Frank Sinatra','x')"
        )
    assert await db.canonical_artist("Beatles - Topic") == "The Beatles"
    assert await db.canonical_artist("Sinatra - Topic") == "Frank Sinatra"


async def test_the_day_list_shows_no_suffix(db, bus):
    await share(db, DAY, A, "alice", title="Coconut", artist="Harry Nilsson - Topic")
    async with client_for(page_app(db, bus)) as client:
        day = await client.get(f"/archive/{DAY}")
    assert "Harry Nilsson" in day.text and "- Topic" not in day.text


async def test_migration_15_cleans_old_rows_and_merged_names(tmp_path):
    path = tmp_path / "old.db"
    db = Database(path)
    await db.connect()
    rows = [await share(db, DAY, v, "alice") for v in (A, B, C, D)]
    for row, artist in zip(rows, ("Nilsson - Topic", "Release - Topic",
                                  "Sinatra - Topic", "Bon Iver"), strict=True):
        await _plant(db, row["id"], artist)
    async with db.transaction():
        await db.db.execute(
            "INSERT INTO artist_aliases(alias,canonical,created_at) VALUES "
            "('Sinatra','Frank Sinatra','x'), ('Iver','Bon Iver - Topic','x'), "
            "('Bon Iver','Bon Iver - Topic','x'), ('nilsson','Nilsson','x')"
        )
    await db.db.execute(f"PRAGMA user_version={len(MIGRATIONS) - 1}")
    await db.close()

    db = Database(path)
    await db.connect()
    try:
        artists = [(await db.track_by_id(r["id"]))["artist"] for r in rows]
        # The stand-in stays for the Fix artists screen; a merged name is
        # followed once its suffix is gone.
        assert artists == ["Nilsson", "Release - Topic", "Frank Sinatra", "Bon Iver"]
        aliases = {(a["alias"], a["canonical"]) for a in await db.artist_aliases()}
        # An alias the suffix made point at itself goes; a case fold stays.
        assert aliases == {("Sinatra", "Frank Sinatra"), ("Iver", "Bon Iver"),
                           ("nilsson", "Nilsson")}
    finally:
        await db.close()
