"""Link extraction and theme detection — pure functions, unit-tested.

Theme detection matches how the Austin #music channel actually posts
(observed via CoreScope): "Happy Friday Music Meshers! Today's theme is:
Friends and friendship." — i.e. the word "theme", optionally a few words,
then a colon, then the title. This also covers the stricter ``Theme: ...``
convention proposed in architecture §6. Every YouTube/YouTube Music link
message attaches to the most recent theme of that day.

The colon has to be a real delimiter: channel messages are full of emoticons,
links, and times, and a colon inside one of those is punctuation. Reading it
as the delimiter is how the day of 2026-07-30 came to be titled "-) or
trains? (Which I really like)" — and because the first theme of a day locks,
a wrong title can only be undone out of band. Declaring nothing is the
cheaper mistake: the day keeps its ``Untitled —`` placeholder, and a later,
clearer post adopts it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# An 11-char YouTube video id.
_ID = r"[A-Za-z0-9_-]{11}"

# Ordered patterns; each must capture the video id in group 1.
_LINK_PATTERNS = [
    # youtu.be/<id>
    re.compile(rf"(?:https?://)?youtu\.be/({_ID})", re.IGNORECASE),
    # {music.,www.,m.,}youtube.com/watch?...v=<id> (v= as first or later param)
    re.compile(
        rf"(?:https?://)?(?:music\.|www\.|m\.)?youtube\.com/watch\?(?:[^\s]*&)?v=({_ID})",
        re.IGNORECASE,
    ),
    # youtube.com/shorts/<id>
    re.compile(
        rf"(?:https?://)?(?:www\.|m\.)?youtube\.com/shorts/({_ID})", re.IGNORECASE
    ),
]

_THEME_WORD = re.compile(r"\btheme", re.IGNORECASE)

# How far past "theme" a title-delimiting colon may sit — room for filler
# words ("is", "for today", "of the day") and not much else.
_FILLER_MAX = 40

# Faces whose mouth is punctuation (":-)", ":(", ":/", ":|"). Nothing legible
# starts with those characters, so these are safe to spot anywhere — even
# jammed against a word, as in "planes:-)". ":/" doubles as the URL-scheme
# colon, which keeps "theme is this https://youtu.be/…" from titling the day
# "//youtu.be/…".
_FACE_PUNCT = re.compile(r":[-^o]?[)(\[\]|/\\]")
# Faces whose mouth is a letter or digit (":D", ":P", ":3", ":*"). These are
# only faces when the colon starts its own token — otherwise "Theme:dance"
# and "Theme:optimism" would read as ":d" and ":o", costing a real title.
_FACE_LETTER = re.compile(r":[-^]?[DdPpOo3*]")


def _delimits_title(text: str, i: int) -> bool:
    """Is the colon at ``i`` the one that introduces the title, or is it just
    punctuation — an emoticon, a URL scheme, a clock time?"""
    if _FACE_PUNCT.match(text, i):
        return False
    if (i == 0 or text[i - 1].isspace()) and _FACE_LETTER.match(text, i):
        return False
    # A clock only when there are digits on both sides, so "day 3: water"
    # still delimits.
    if 0 < i < len(text) - 1 and text[i - 1].isdigit() and text[i + 1].isdigit():
        return False
    return True


def _title_after(text: str, pos: int) -> str | None:
    """Title declared by the colon that follows ``pos``, if any.

    Looks only within the filler window and only on that line, skipping
    colons that are punctuation. Skipping (rather than giving up at the first
    colon) is what keeps a stray ":-)" from swallowing the title; finding no
    real colon at all means this "theme" mention declares nothing."""
    line_end = text.find("\n", pos)
    if line_end == -1:
        line_end = len(text)
    # +1 because the colon may sit just past the last filler character.
    limit = min(pos + _FILLER_MAX + 1, line_end)

    i = text.find(":", pos, limit)
    while i != -1:
        if _delimits_title(text, i):
            return text[i + 1 : line_end].strip().rstrip(".!?…").strip() or None
        i = text.find(":", i + 1, limit)
    return None


@dataclass(frozen=True)
class ParsedLink:
    video_id: str
    url: str  # canonical watch URL, what yt-dlp gets fed


def canonical_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def extract_links(text: str) -> list[ParsedLink]:
    """All YouTube/YT Music video links in a message, deduplicated, in order."""
    seen: set[str] = set()
    links: list[ParsedLink] = []
    for pattern in _LINK_PATTERNS:
        for match in pattern.finditer(text):
            vid = match.group(1)
            if vid not in seen:
                seen.add(vid)
                links.append(ParsedLink(video_id=vid, url=canonical_url(vid)))
    return links


def parse_theme(text: str) -> str | None:
    """Theme title if this message declares one.

    Matches ``Theme: songs about rain`` and in-the-wild phrasings like
    ``Today's theme is: Friends and friendship.`` Trailing sentence
    punctuation is stripped so repeat posts dedupe to the same title.

    A message can say "theme" without declaring one ("no theme yet?"), so
    each mention is tried in turn and the first that carries a real title
    wins. A mention with no delimiting colon declares nothing — the day then
    keeps its ``Untitled —`` placeholder, which a later, clearer post can
    still adopt. That is the safe failure: the day's theme locks on whatever
    lands first, and a wrong title can only be undone out of band
    (``meshradio --set-theme``).
    """
    for mention in _THEME_WORD.finditer(text):
        title = _title_after(text, mention.end())
        if title:
            return title
    return None


def untitled_theme(date: str) -> str:
    """Fallback theme title for days where nobody posted one."""
    return f"Untitled — {date}"
