"""Channel message parsing: the YouTube link forms people paste, and the
phrasings that do (and do not) declare a day's theme."""

import pytest

from meshradio.ingest.parse import extract_links, parse_theme, untitled_theme

VID = "dQw4w9WgXcQ"
OTHER = "abcdefghijk"

# -- links ---------------------------------------------------------------------


@pytest.mark.parametrize("text, ids", [
    pytest.param(f"check this out https://youtu.be/{VID}", [VID], id="youtu.be"),
    pytest.param(f"https://www.youtube.com/watch?v={VID}", [VID], id="watch"),
    pytest.param(f"https://music.youtube.com/watch?v={VID}&si=abc123", [VID], id="music"),
    pytest.param(f"https://www.youtube.com/watch?list=PLxyz&v={VID}", [VID], id="v not first"),
    pytest.param(f"https://youtube.com/shorts/{VID}", [VID], id="shorts"),
    pytest.param(f"youtu.be/{VID} is a banger", [VID], id="no scheme"),
    pytest.param(f"https://youtu.be/{VID} and again https://www.youtube.com/watch?v={VID}",
                 [VID], id="same video twice is one link"),
    pytest.param(f"https://youtu.be/{VID}\nhttps://youtu.be/{OTHER}", [VID, OTHER],
                 id="two videos, posted order"),
    pytest.param("just chatting, no links here", [], id="no links"),
    pytest.param("https://youtu.be/short", [], id="an id is exactly 11 chars"),
])
def test_links_are_found_in_every_form_people_paste(text, ids):
    assert [link.video_id for link in extract_links(text)] == ids


def test_links_are_canonicalised():
    (link,) = extract_links(f"music.youtube.com/watch?v={VID}")
    assert link.url == f"https://www.youtube.com/watch?v={VID}"


# -- themes --------------------------------------------------------------------


@pytest.mark.parametrize("text, title", [
    pytest.param("Theme: songs about rain", "songs about rain", id="basic"),
    # Actual message observed on the Austin #music channel via CoreScope.
    pytest.param("Happy Friday Music Meshers! Today’s theme is: Friends and friendship.",
                 "Friends and friendship", id="real Austin phrasing"),
    pytest.param("theme for today: disco or funk", "disco or funk", id="for today"),
    pytest.param("Theme: songs about rain!", "songs about rain", id="trailing punctuation"),
    pytest.param("THEME:  One Hit Wonders ", "One Hit Wonders", id="case and spacing"),
    pytest.param("good morning!\ntheme: covers better than the original",
                 "covers better than the original", id="on its own line mid-message"),
    pytest.param("is there a theme today?\nTheme: one hit wonders", "one hit wonders",
                 id="second mention wins when the first declares nothing"),
    pytest.param("morning :-) today's theme is: rain", "rain",
                 id="an emoticon before the real colon"),
    # Past the delimiter it's just title text, punctuation and all.
    pytest.param("Theme: songs that make you go :-)", "songs that make you go :-)",
                 id="an emoticon inside the title"),
    pytest.param("theme at 8:30 tonight is: slow jams", "slow jams",
                 id="a clock time before the real colon"),
    # Only a digit on *both* sides means a clock, so these are real titles.
    pytest.param("theme for day 3: water", "water", id="numbered day"),
    pytest.param("Theme:80s hits", "80s hits", id="title starts with a digit"),
    # ":D"/":P"/":o" only count as faces when the colon starts its own token —
    # otherwise "Theme:dance" would lose its title to a phantom smiley.
    *[pytest.param(f"Theme:{t}", t, id=f"unspaced {t!r} is not a face")
      for t in ("dance", "optimism", "pop punk", "3 chord songs", "Dad rock")],
])
def test_theme_phrasings_that_declare_a_title(text, title):
    assert parse_theme(text) == title


@pytest.mark.parametrize("text", [
    pytest.param("great tune https://youtu.be/dQw4w9WgXcQ", id="no theme at all"),
    pytest.param("Theme:   ", id="empty title"),
    # The 2026-07-30 regression: a colon-less "theme is …" plus a smiley left
    # the day titled "-) or trains? (Which I really like)".
    pytest.param("Today's theme is planes :-) or trains? (Which I really like)",
                 id="2026-07-30 smiley regression"),
    *[pytest.param(f"today's theme is trains {face} anyway", id=f"emoticon {face}")
      for face in (":-)", ":)", ":D", ":P", ":/", ":|", ":3", ":-(")],
    pytest.param("today's theme is planes:-) or trains", id="a face jammed against a word"),
    pytest.param(f"todays theme is this one https://youtu.be/{VID}", id="a URL scheme"),
    pytest.param("theme at 8:30 tonight", id="a clock time"),
    # The colon has to be near "theme" — otherwise any later sentence with a
    # colon would retitle the day.
    pytest.param("theme " + "x" * 60 + ": not the title", id="a colon far from the word"),
])
def test_mentions_that_declare_nothing(text):
    assert parse_theme(text) is None


def test_untitled_theme():
    assert untitled_theme("2026-07-06") == "Untitled — 2026-07-06"
