"""One SQLite connection shared by every coroutine: writes must not be able
to step on each other.

The driver's implicit transactions once let a rollback in one task discard
another task's uncommitted UPDATE (a dedupe rollback in add_track un-readied
a track the cacher had just finished). Writers now serialise behind
``Database.transaction()``; these tests pin that contract, and the batching
that the same helper makes possible."""

import asyncio

import pytest

from meshradio.runtime import spawn

from .helpers import make_ready_on


async def add(db, video_id, theme_id, sender="alice"):
    return await db.add_track(
        video_id=video_id, url="u", channel="#music", sender=sender,
        mesh_ts=1_783_400_000.0, source="mesh", theme_id=theme_id,
    )


async def test_rollback_in_one_task_cannot_discard_anothers_write(db):
    """Task A opens a transaction, yields, then fails and rolls back. Task B
    writes in the gap. B's write must survive: it waited for A's lock rather
    than landing inside A's transaction."""
    a = await make_ready_on(db, "aaaaaaaaaaa", "2026-07-06")
    b = await make_ready_on(db, "bbbbbbbbbbb", "2026-07-06")
    await db.set_cache_status(b["id"], "pending")

    async def task_a():
        with pytest.raises(RuntimeError):
            async with db.transaction():
                await db.db.execute(
                    "UPDATE tracks SET cache_status='failed' WHERE id=?", (a["id"],)
                )
                await asyncio.sleep(0.05)      # the await gap the race needs
                raise RuntimeError("boom")

    async def task_b():
        await asyncio.sleep(0.01)              # start inside A's gap
        await db.set_cache_status(b["id"], "ready", "/x")

    await asyncio.gather(task_a(), task_b())
    assert (await db.track_by_id(a["id"]))["cache_status"] == "ready"   # A undone
    assert (await db.track_by_id(b["id"]))["cache_status"] == "ready"   # B kept


async def test_batch_commits_once_and_a_dedupe_inside_it_does_not_abort_it(db):
    async with db.transaction():
        theme = await db.create_theme("2026-07-06", "rain")
        assert await add(db, "aaaaaaaaaaa", theme["id"])
        assert await add(db, "aaaaaaaaaaa", theme["id"], sender="bob") is None  # repost
        assert await add(db, "bbbbbbbbbbb", theme["id"])
        await db.set_setting("k", "v")         # nested write joins the batch
    assert len(await db.tracks_for_theme(theme["id"])) == 2
    assert await db.get_setting("k") == "v"


async def test_failed_batch_leaves_nothing_behind(db):
    with pytest.raises(RuntimeError):
        async with db.transaction():
            theme = await db.create_theme("2026-07-06", "rain")
            await add(db, "aaaaaaaaaaa", theme["id"])
            await db.set_setting("k", "v")
            raise RuntimeError("halfway")
    assert await db.themes_for_day("2026-07-06") == []
    assert await db.get_setting("k") is None
    # And the connection is usable afterwards, outside any transaction.
    assert await db.create_theme("2026-07-06", "rain")


async def test_transaction_is_owned_by_the_entering_task(db):
    """A task spawned inside a transaction inherits the context but not the
    transaction: it must wait for the lock, not write into the parent's
    (still uncommitted, maybe about to roll back) group."""
    order: list[str] = []

    async def child():
        await db.set_setting("child", "wrote")
        order.append("child")

    async with db.transaction():
        task = spawn("child", child())
        await asyncio.sleep(0.05)
        assert not task.done()                 # blocked on the lock
        order.append("parent")
    await task
    assert order == ["parent", "child"]
    assert await db.get_setting("child") == "wrote"
