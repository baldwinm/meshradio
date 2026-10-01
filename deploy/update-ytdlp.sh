#!/bin/sh
# Upgrade yt-dlp inside MeshRadio's virtualenv. Run nightly by
# meshradio-ytdlp-update.timer; safe to run by hand.
#
# YouTube changes something every few weeks and yt-dlp fixes it within
# days, so a radio that never updates stops being able to cache new songs.
# The running service needs no restart: yt-dlp is a subprocess, so the next
# download uses the new version. The version in play shows in /healthz.
#
# A venv made with `uv venv` has no pip, one made with `python -m venv`
# does; use whichever the venv has.
set -eu
VENV="${MESHRADIO_VENV:-/home/pi/meshradio/.venv}"
if command -v uv >/dev/null 2>&1; then
    exec uv pip install --python "$VENV/bin/python" --upgrade yt-dlp
elif [ -x "$VENV/bin/pip" ]; then
    exec "$VENV/bin/pip" install --quiet --upgrade yt-dlp
else
    echo "no uv on PATH and no pip in $VENV; can't upgrade yt-dlp" >&2
    exit 1
fi
