"""Link extraction and theme detection — pure functions, unit-tested.

Theme detection matches how the Austin #music channel actually posts
(observed via CoreScope): "Happy Friday Music Meshers! Today's theme is:
Friends and friendship." — i.e. the word "theme", optionally a few words,
then a colon, then the title. This also covers the stricter ``Theme: ...``
convention proposed in architecture §6. Every YouTube/YouTube Music link
message attaches to the most recent theme of that day.
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

# "theme", at most a few filler words ("is", "for today", …), a colon, title.
_THEME_RE = re.compile(r"\btheme[^:\n]{0,40}:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


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
    """
    match = _THEME_RE.search(text)
    if match:
        title = match.group(1).strip().rstrip(".!?…").strip()
        return title or None
    return None


def untitled_theme(date: str) -> str:
    """Fallback theme title for days where nobody posted one."""
    return f"Untitled — {date}"
