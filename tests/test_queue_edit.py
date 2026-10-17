"""Queue Edit mode: songs picked by track id, moved, removed, played next,
cleared, and each change undone, including after the queue has moved on."""

from .helpers import client_for, make_player, make_ready_on, page_app


def queued(*ids, source="corescope"):
    return [{"id": i, "video_id": f"v{i}", "source": source} for i in ids]


def ids_of(player):
    return [t["id"] for t in player.queue]


async def test_remove_tracks_takes_out_every_picked_song_and_undoes(db, bus):
    player = make_player(db, bus)
    player.queue = queued(1, 2, 3, 4)
    count, token = await player.remove_tracks([3, 1, 99])   # 99 isn't queued
    assert count == 2 and ids_of(player) == [2, 4]
    assert await player.undo_edit(token) is True
    assert ids_of(player) == [1, 2, 3, 4]
    assert await player.undo_edit(token) is False            # one undo per edit


async def test_play_next_keeps_the_picked_songs_in_queue_order(db, bus):
    player = make_player(db, bus)
    player.queue = queued(1, 2, 3, 4, 5)
    count, token = await player.play_next([5, 3])
    assert count == 2 and ids_of(player) == [3, 5, 1, 2, 4]
    # Already at the front: nothing to undo.
    assert await player.play_next([3, 5]) == (2, None)
    assert await player.undo_edit(token) is True
    assert ids_of(player) == [1, 2, 3, 4, 5]


async def test_move_track_lands_a_song_at_its_new_place(db, bus):
    player = make_player(db, bus)
    player.queue = queued(1, 2, 3, 4)
    token = await player.move_track(1, 2)
    assert ids_of(player) == [2, 3, 1, 4]
    assert await player.move_track(2, 99) is not None       # clamped to the end
    assert ids_of(player) == [3, 1, 4, 2]
    assert await player.move_track(2, 3) is None             # already there
    assert await player.move_track(42, 0) is None            # not queued
    assert token is not None


async def test_undo_leaves_the_song_that_started_playing_and_keeps_new_arrivals(db, bus):
    player = make_player(db, bus)
    player.queue = queued(1, 2, 3)
    _, token = await player.remove_tracks([2])
    # Since then: song 1 started playing, and song 9 was posted.
    player.current = player.queue.pop(0)
    player.queue.append(queued(9)[0])
    assert await player.undo_edit(token) is True
    assert ids_of(player) == [2, 3, 9]


async def test_clear_from_edit_mode_can_be_undone(db, bus):
    player = make_player(db, bus)
    player.queue = queued(1, 2)
    player.station = "archive"
    token = await player.clear_queue_undoable()
    assert player.queue == [] and player.station is None
    assert await player.undo_edit(token) is True
    assert ids_of(player) == [1, 2]
    assert player.station is None                            # the station stays off


async def test_the_edit_routes_answer_with_the_queue_and_an_undo_token(db, bus):
    for video_id in ("aaaaaaaaaaa", "bbbbbbbbbbb", "ccccccccccc", "ddddddddddd"):
        await make_ready_on(db, video_id, "2026-08-01")
    async with client_for(page_app(db, bus)) as client:
        await client.post("/api/play-day/2026-08-01")
        queue = (await client.get("/api/state")).json()["queue"]
        first, second, third = (t["id"] for t in queue)

        resp = await client.post(f"/api/queue/edit/remove?ids={first},{third}")
        assert resp.status_code == 200
        assert resp.headers["X-Queue-Count"] == "2"
        token = resp.headers["X-Queue-Undo"]
        assert f'data-track-id="{second}"' in resp.text
        assert f'data-track-id="{first}"' not in resp.text

        resp = await client.post(f"/api/queue/edit/undo/{token}")
        assert f'data-track-id="{first}"' in resp.text and "X-Queue-Undo" not in resp.headers

        resp = await client.post(f"/api/queue/edit/move/{third}/0")
        assert resp.headers.get("X-Queue-Undo")
        state = (await client.get("/api/state")).json()
        assert [t["id"] for t in state["queue"]] == [third, first, second]

        resp = await client.post(f"/api/queue/edit/next?ids={second}")
        assert resp.headers["X-Queue-Count"] == "1"

        resp = await client.post("/api/queue/edit/clear")
        assert resp.headers["X-Queue-Count"] == "3" and resp.headers.get("X-Queue-Undo")


async def test_the_edit_routes_refuse_what_isnt_a_list_of_ids(db, bus):
    async with client_for(page_app(db, bus)) as client:
        assert (await client.post("/api/queue/edit/remove?ids=1,two")).status_code == 422
        assert (await client.post("/api/queue/edit/remove?ids=")).status_code == 422
        many = ",".join(str(i) for i in range(600))
        assert (await client.post(f"/api/queue/edit/next?ids={many}")).status_code == 422
        assert (await client.post("/api/queue/edit/move/1/-1")).status_code == 422
