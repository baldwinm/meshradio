import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from meshradio.bus import EventBus, THEME_CREATED, TRACK_DISCOVERED
from meshradio.db import Database
from meshradio.ingest.service import IngestService

VID = "dQw4w9WgXcQ"
CHICAGO = ZoneInfo("America/Chicago")
NOON_CDT = datetime(2026, 7, 6, 12, 0, tzinfo=CHICAGO).timestamp()


def make_service(db: Database, bus: EventBus) -> IngestService:
    return IngestService(db, bus, channel="#music", tz="America/Chicago")


async def test_theme_message_creates_theme(db, bus):
    service = make_service(db, bus)
    sub = bus.subscribe(THEME_CREATED)
    await service.handle_message(
        sender="alice", text="Theme: songs about rain", ts=NOON_CDT, source="mesh"
    )
    _, payload = await asyncio.wait_for(sub.get(), 1)
    assert payload["theme"]["title"] == "songs about rain"
    assert payload["theme"]["set_by"] == "alice"


async def test_link_attaches_to_latest_theme(db, bus):
    service = make_service(db, bus)
    await service.handle_message(
        sender="alice", text="Theme: rain", ts=NOON_CDT, source="mesh"
    )
    sub = bus.subscribe(TRACK_DISCOVERED)
    inserted = await service.handle_message(
        sender="bob", text=f"https://youtu.be/{VID}", ts=NOON_CDT + 600, source="mesh"
    )
    assert inserted == 1
    _, payload = await asyncio.wait_for(sub.get(), 1)
    theme = await db.theme_by_id(payload["track"]["theme_id"])
    assert theme["title"] == "rain"


async def test_link_without_theme_creates_untitled(db, bus):
    service = make_service(db, bus)
    await service.handle_message(
        sender="bob", text=f"https://youtu.be/{VID}", ts=NOON_CDT, source="corescope"
    )
    themes = await db.themes_for_day("2026-07-06")
    assert len(themes) == 1
    assert themes[0]["title"].startswith("Untitled")


async def test_dedupe_across_sources(db, bus):
    service = make_service(db, bus)
    text = f"https://youtu.be/{VID}"
    first = await service.handle_message(sender="bob", text=text, ts=NOON_CDT, source="mesh")
    second = await service.handle_message(
        sender="bob", text=text, ts=NOON_CDT + 10, source="corescope"
    )
    assert first == 1
    assert second == 0  # same 60s bucket -> deduped


async def test_theme_message_with_link_ingests_both(db, bus):
    service = make_service(db, bus)
    inserted = await service.handle_message(
        sender="alice",
        text=f"Theme: bangers\nhttps://youtu.be/{VID}",
        ts=NOON_CDT,
        source="mesh",
    )
    assert inserted == 1
    themes = await db.themes_for_day("2026-07-06")
    assert themes[0]["title"] == "bangers"
    tracks = await db.tracks_for_theme(themes[0]["id"])
    assert len(tracks) == 1


async def test_local_date_rollover(db, bus):
    service = make_service(db, bus)
    # 22:30 in Chicago is already 2026-07-07 in UTC (CDT = UTC-5)
    late_evening = datetime(2026, 7, 6, 22, 30, tzinfo=CHICAGO).timestamp()
    assert datetime.fromtimestamp(late_evening).astimezone(ZoneInfo("UTC")).date().isoformat() == "2026-07-07"
    assert service.local_date(late_evening) == "2026-07-06"
