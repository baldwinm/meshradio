"""What code is running, and what the Pi's auto-updater last did.

deploy/auto-update.sh leaves a small JSON report in its state directory on
every run (``status.json``): when it checked, what happened, and the commit
the clone is on. This module reads that report, tidies anything that arrives
from outside (the Pi relays its report to the hosted site with each push),
and turns it into the one line the admin overview and /healthz show.
"""

from __future__ import annotations

import json
import os
import re
from functools import cache
from pathlib import Path
from typing import Any

from . import __version__

# Where auto-update.sh writes (systemd's StateDirectory=meshradio-autoupdate).
# The env var is for tests and unusual installs.
STATUS_PATH = "/var/lib/meshradio-autoupdate/status.json"

REPO_URL = "https://github.com/baldwinm/meshradio"

# The timer runs every ten minutes; three missed runs is a timer that isn't
# running (disabled, removed, or the Pi's clock or systemd in trouble).
STALE_S = 30 * 60

# Both the site and the Pi follow main once CI passes, so after a merge they
# should match within one of the Pi's ten-minute checks plus a restart. On
# a different commit for longer than this, one of them isn't keeping up.
LAG_S = 30 * 60

# What auto-update.sh reports, and what each means for "is it working".
_RESULTS = {
    "current": ("ok", "Up to date as of its last check"),
    "updated": ("ok", "Updated and healthy"),
    "fast-forwarded": ("ok", "Updated (nothing the radio runs changed, so no restart)"),
    "no-branch": ("ok", "Waiting for CI to publish the first deploy commit"),
    "skipped-bad": ("bad", "Holding back a commit that failed its health check; "
                           "waiting for the next green commit"),
    "rolled-back": ("bad", "The new commit didn't come up healthy and was rolled back"),
    "blocked": ("bad", "Can't update the clone"),
    "failed": ("bad", "The update run failed"),
}

_SHA = re.compile(r"[0-9a-f]{7,40}")
_VERSION = re.compile(r"[0-9A-Za-z.+-]{1,32}")


def _sha(value: Any) -> str | None:
    return value if isinstance(value, str) and _SHA.fullmatch(value) else None


def _time(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if 0 < value < 1e11 else None


def clean_status(raw: Any) -> dict[str, Any] | None:
    """An auto-update report with only the fields we know, each checked; None
    if it isn't one. Applied to the local file and to what a relay sends."""
    if not isinstance(raw, dict) or raw.get("result") not in _RESULTS:
        return None
    checked_at = _time(raw.get("checked_at"))
    if checked_at is None:
        return None
    message = raw.get("message")
    return {
        "result": raw["result"],
        "message": message[:300] if isinstance(message, str) else "",
        "checked_at": checked_at,
        "updated_at": _time(raw.get("updated_at")),
        "commit": _sha(raw.get("commit")),
        "target": _sha(raw.get("target")),
    }


def read_status(path: str | None = None) -> dict[str, Any] | None:
    """This machine's last auto-update report, or None when there isn't one
    (no timer installed here, or it hasn't run since it learned to report)."""
    path = path or os.environ.get("MESHRADIO_AUTOUPDATE_STATUS") or STATUS_PATH
    try:
        return clean_status(json.loads(Path(path).read_text()))
    except (OSError, ValueError):
        return None


def _git_head(root: Path) -> str | None:
    """The commit a clone has checked out, read straight from .git (no git
    binary needed, and nothing to spawn)."""
    git = root / ".git"
    try:
        head = (git / "HEAD").read_text().strip()
        if not head.startswith("ref: "):
            return _sha(head)
        ref = head[5:]
        if (git / ref).is_file():
            return _sha((git / ref).read_text().strip())
        for line in (git / "packed-refs").read_text().splitlines():
            sha, _, name = line.partition(" ")
            if name == ref:
                return _sha(sha)
    except OSError:
        pass
    return None


@cache
def running_commit() -> str | None:
    """The commit this process was started from. Render names it in the
    environment; the Pi runs from a clone (an editable install), so it's
    whatever the clone had checked out at startup — the updater restarts the
    service after every change the radio runs. Cached: the code in memory
    doesn't change under a running process."""
    for name in ("MESHRADIO_COMMIT", "RENDER_GIT_COMMIT"):
        value = _sha(os.environ.get(name, "").strip().lower())
        if value:
            return value
    return _git_head(Path(__file__).resolve().parents[1])


def node_info() -> dict[str, Any]:
    """What a relay tells the hosted site about the node it runs on."""
    return {"version": __version__, "commit": running_commit(), "autoupdate": read_status()}


def clean_node(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    version = raw.get("version")
    return {
        "version": version if isinstance(version, str) and _VERSION.fullmatch(version) else None,
        "commit": _sha(raw.get("commit")),
        "autoupdate": clean_status(raw.get("autoupdate")),
    }


def deployed_commit(node: dict[str, Any], now: float) -> str | None:
    """The commit a relayed node has deployed: its clone's, from a fresh
    updater report (an update touching nothing the radio runs moves the
    clone without a restart), else the one it started from — a report left
    behind by a timer that stopped says nothing about a later update by
    hand."""
    report = node.get("autoupdate")
    if report and report.get("commit") and now - report["checked_at"] <= STALE_S:
        return report["commit"]
    return node.get("commit")


def _same_commit(a: str, b: str) -> bool:
    return a.startswith(b) or b.startswith(a)


def track_mismatch(previous: dict[str, Any] | None, node: dict[str, Any],
                   site_commit: str | None, now: float) -> dict[str, Any] | None:
    """Since when the Pi has sat on a commit other than this site's, as
    ``{"pi": commit, "since": time}``; None while they match or either is
    unknown. The clock restarts only when the Pi moves (a Pi that moved is
    updating, however many merges came in between) — never because this
    site redeployed, which a stuck Pi would otherwise hide behind every
    merge. The caller keeps the record across restarts."""
    pi = deployed_commit(node, now)
    if not pi or not site_commit or _same_commit(pi, site_commit):
        return None
    if previous and previous.get("pi") == pi and isinstance(previous.get("since"), (int, float)):
        return previous
    return {"pi": pi, "since": now}


def describe(status: dict[str, Any] | None, now: float) -> dict[str, Any]:
    """The overview's line for an auto-update report: ``state`` is ok, bad or
    none (no report at all), ``text`` says what happened, ``detail`` is the
    script's own words."""
    if status is None:
        return {"state": "none", "ok": None, "age": None, "text": "No report yet", "detail": ""}
    age = max(0.0, now - status["checked_at"])
    state, text = _RESULTS[status["result"]]
    if age > STALE_S:
        state = "bad"
        text = "Hasn't checked for updates lately; is meshradio-autoupdate.timer running?"
    return {"state": state, "ok": state == "ok", "age": age, "text": text,
            "detail": status["message"]}


def health_view(status: dict[str, Any] | None, now: float,
                mismatch_since: float | None = None) -> dict[str, Any] | None:
    """The part of a report /healthz shows (it's public: no messages).
    ``mismatch_since`` comes from a relay receiver (see track_mismatch): a
    Pi on a different commit from this site for longer than LAG_S isn't ok
    either, whatever its updater says (the deploy branch may have stopped
    moving, or this site's deploys have)."""
    if status is None:
        return None
    view = describe(status, now)
    mismatch = round(now - mismatch_since, 1) if mismatch_since is not None else None
    return {
        "ok": bool(view["ok"]) and not (mismatch is not None and mismatch > LAG_S),
        "result": status["result"],
        "checked_age_s": round(view["age"], 1),
        "commit": status["commit"][:7] if status["commit"] else None,
        "site_mismatch_s": mismatch,
    }
