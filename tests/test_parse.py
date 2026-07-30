import pytest

from meshradio.ingest.parse import extract_links, parse_theme, untitled_theme

VID = "dQw4w9WgXcQ"


def test_youtu_be():
    links = extract_links(f"check this out https://youtu.be/{VID}")
    assert [l.video_id for l in links] == [VID]


def test_watch_url():
    links = extract_links(f"https://www.youtube.com/watch?v={VID}")
    assert [l.video_id for l in links] == [VID]


def test_music_youtube():
    links = extract_links(f"https://music.youtube.com/watch?v={VID}&si=abc123")
    assert [l.video_id for l in links] == [VID]


def test_v_not_first_param():
    links = extract_links(f"https://www.youtube.com/watch?list=PLxyz&v={VID}")
    assert [l.video_id for l in links] == [VID]


def test_shorts():
    links = extract_links(f"https://youtube.com/shorts/{VID}")
    assert [l.video_id for l in links] == [VID]


def test_no_scheme():
    links = extract_links(f"youtu.be/{VID} is a banger")
    assert [l.video_id for l in links] == [VID]


def test_multiple_links_deduped():
    text = f"https://youtu.be/{VID} and again https://www.youtube.com/watch?v={VID}"
    links = extract_links(text)
    assert len(links) == 1


def test_two_distinct_links():
    other = "abcdefghijk"
    text = f"https://youtu.be/{VID}\nhttps://youtu.be/{other}"
    links = extract_links(text)
    assert [l.video_id for l in links] == [VID, other]


def test_canonical_url():
    links = extract_links(f"music.youtube.com/watch?v={VID}")
    assert links[0].url == f"https://www.youtube.com/watch?v={VID}"


def test_no_links():
    assert extract_links("just chatting, no links here") == []


def test_not_a_video_id():
    # id must be exactly 11 chars
    assert extract_links("https://youtu.be/short") == []


def test_theme_basic():
    assert parse_theme("Theme: songs about rain") == "songs about rain"


def test_theme_real_austin_phrasing():
    # Actual message observed on the Austin #music channel via CoreScope.
    text = "Happy Friday Music Meshers! Today’s theme is: Friends and friendship."
    assert parse_theme(text) == "Friends and friendship"


def test_theme_for_today_variant():
    assert parse_theme("theme for today: disco or funk") == "disco or funk"


def test_theme_trailing_punctuation_stripped():
    assert parse_theme("Theme: songs about rain!") == "songs about rain"


def test_theme_case_insensitive():
    assert parse_theme("THEME:  One Hit Wonders ") == "One Hit Wonders"


def test_theme_not_present():
    assert parse_theme("great tune https://youtu.be/dQw4w9WgXcQ") is None


def test_theme_mid_message_line():
    # Theme declared on its own line inside a longer message still counts.
    assert parse_theme("good morning!\ntheme: covers better than the original") == (
        "covers better than the original"
    )


def test_theme_empty_title():
    assert parse_theme("Theme:   ") is None


def test_theme_emoticon_is_not_a_delimiter():
    """The 2026-07-30 regression: a colon-less "theme is …" plus a smiley left
    the day titled "-) or trains? (Which I really like)". A mention with no
    real colon declares nothing."""
    text = "Today's theme is planes :-) or trains? (Which I really like)"
    assert parse_theme(text) is None


@pytest.mark.parametrize("face", [":-)", ":)", ":D", ":P", ":/", ":|", ":3", ":-("])
def test_theme_emoticons_never_delimit(face):
    assert parse_theme(f"today's theme is trains {face} anyway") is None


def test_theme_survives_an_emoticon_before_the_real_colon():
    assert parse_theme("morning :-) today's theme is: rain") == "rain"


def test_theme_keeps_an_emoticon_inside_the_title():
    # Past the delimiter it's just title text, punctuation and all.
    assert parse_theme("Theme: songs that make you go :-)") == "songs that make you go :-)"


def test_theme_url_scheme_is_not_a_delimiter():
    text = f"todays theme is this one https://youtu.be/{VID}"
    assert parse_theme(text) is None


def test_theme_clock_time_is_not_a_delimiter():
    assert parse_theme("theme at 8:30 tonight") is None
    assert parse_theme("theme at 8:30 tonight is: slow jams") == "slow jams"


def test_theme_numbered_title_still_delimits():
    # Only a digit on *both* sides means a clock, so these are real titles.
    assert parse_theme("theme for day 3: water") == "water"
    assert parse_theme("Theme:80s hits") == "80s hits"


@pytest.mark.parametrize("title", ["dance", "optimism", "pop punk", "3 chord songs", "Dad rock"])
def test_theme_unspaced_title_is_not_read_as_a_face(title):
    """":D"/":P"/":o" only count as faces when the colon starts its own token —
    otherwise "Theme:dance" would lose its title to a phantom smiley."""
    assert parse_theme(f"Theme:{title}") == title


def test_theme_face_jammed_against_a_word_still_skipped():
    assert parse_theme("today's theme is planes:-) or trains") is None


def test_theme_second_mention_wins_when_the_first_declares_nothing():
    text = "is there a theme today?\nTheme: one hit wonders"
    assert parse_theme(text) == "one hit wonders"


def test_theme_far_from_the_colon_is_ignored():
    # The colon has to be near "theme" — otherwise any later sentence with a
    # colon would retitle the day.
    text = "theme " + "x" * 60 + ": not the title"
    assert parse_theme(text) is None


def test_untitled_theme():
    assert untitled_theme("2026-07-06") == "Untitled — 2026-07-06"
