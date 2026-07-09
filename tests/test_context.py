"""Archive calendar grouping (meshradio.web.context.archive_calendar)."""

from meshradio.web.context import archive_calendar


def _day(date, tracks=1, themes=1):
    return {"date": date, "tracks": tracks, "themes": themes}


def test_empty_input():
    assert archive_calendar([]) == []


def test_groups_by_month_newest_first():
    months = archive_calendar(
        [_day("2026-07-06"), _day("2026-06-30"), _day("2026-07-20")]
    )
    assert [(m["year"], m["month"]) for m in months] == [(2026, 7), (2026, 6)]
    assert months[0]["label"] == "July 2026"
    assert months[1]["label"] == "June 2026"


def test_month_totals():
    month = archive_calendar([_day("2026-07-06", tracks=2), _day("2026-07-20", tracks=5)])[0]
    assert month["tracks"] == 7
    assert month["active_days"] == 2


def test_weeks_are_sunday_first_and_padded():
    month = archive_calendar([_day("2026-07-01")])[0]
    # every week is exactly 7 cells
    assert all(len(week) == 7 for week in month["weeks"])
    # July 1 2026 is a Wednesday -> Sun/Mon/Tue of the first week spill from June
    first = month["weeks"][0]
    assert first[:3] == [None, None, None]
    assert first[3] == {"day": 1, "date": "2026-07-01", "tracks": 1, "active": True}


def test_days_without_an_entry_are_inactive():
    month = archive_calendar([_day("2026-07-06", tracks=3)])[0]
    cells = [c for week in month["weeks"] for c in week if c]
    active = [c for c in cells if c["active"]]
    assert len(active) == 1
    assert active[0]["date"] == "2026-07-06"
    assert active[0]["tracks"] == 3
    # a real, non-archived day in the same month renders but isn't a link
    other = next(c for c in cells if c["date"] == "2026-07-07")
    assert other["active"] is False
    assert other["tracks"] == 0


def test_zero_track_day_still_active():
    # a day with a theme but songs still caching stays linkable
    month = archive_calendar([_day("2026-07-06", tracks=0)])[0]
    cell = next(c for week in month["weeks"] for c in week if c and c["active"])
    assert cell["date"] == "2026-07-06"
    assert cell["tracks"] == 0
    assert month["active_days"] == 1
