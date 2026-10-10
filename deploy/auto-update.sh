#!/bin/sh
# Keep the Pi on the newest commit CI has passed. Run every ten minutes by
# meshradio-autoupdate.timer; safe to run by hand (as root).
#
# CI moves the `pi-deploy` branch to each main commit whose test workflow
# went green (.github/workflows/pi-deploy.yml), so this never pulls a commit
# the suite failed. When that branch is ahead of the clone it:
#
#   1. fast-forwards the clone (as the user who owns it; a clone with local
#      commits or another branch checked out is left alone, and so is one
#      with local edits to a file the update changes — edits elsewhere, such
#      as a unit file adjusted in place, ride along),
#   2. reinstalls the package into the venv, every time — a bare `git pull`
#      once left a stale launcher behind and the service crash-looped,
#   3. restarts the service and waits for /healthz to answer ok,
#   4. and if it doesn't, puts the previous commit back, reinstalls,
#      restarts, and remembers the bad commit so the next run doesn't try it
#      again (the next green commit is tried as normal).
#
# A change that touches nothing the Pi runs (docs, tests, CI, the Render
# files, the lock — the Pi installs from pyproject, not uv.lock) is
# fast-forwarded without a restart, so the music doesn't drop for it.
#
# Every run, including the quiet ones, leaves a report in
# $STATE_DIRECTORY/status.json: when it checked, what happened (result:
# current, updated, fast-forwarded, no-branch, skipped-bad, rolled-back,
# blocked or failed), why, and the commit the clone is on. The radio shows it
# on the admin overview and in /healthz, and relays it to the hosted site.
#
# Settings come from the environment (the unit sets them):
#   MESHRADIO_DIR          the clone                  (/home/pi/meshradio)
#   MESHRADIO_USER         who owns it                (the clone's owner)
#   MESHRADIO_SERVICE      the unit to restart        (meshradio)
#   MESHRADIO_HEALTH_URL   where to check             (http://127.0.0.1:8080/healthz)
#   MESHRADIO_EXTRAS       pip extras                 (media,hw)
#   MESHRADIO_DEPLOY_BRANCH                           (pi-deploy)
#   MESHRADIO_HEALTH_WAIT  seconds to wait for ok     (90)
#   STATE_DIRECTORY        where the report and the bad commit are kept
#                          (systemd sets it)
set -eu

DIR="${MESHRADIO_DIR:-/home/pi/meshradio}"
OWNER="${MESHRADIO_USER:-$(stat -c %U "$DIR")}"
SERVICE="${MESHRADIO_SERVICE:-meshradio}"
HEALTH_URL="${MESHRADIO_HEALTH_URL:-http://127.0.0.1:8080/healthz}"
EXTRAS="${MESHRADIO_EXTRAS:-media,hw}"
BRANCH="${MESHRADIO_DEPLOY_BRANCH:-pi-deploy}"
WAIT="${MESHRADIO_HEALTH_WAIT:-90}"
STATE="${STATE_DIRECTORY:-/var/lib/meshradio-autoupdate}"
BAD="$STATE/bad-commit"
STATUS="$STATE/status.json"

log() { echo "auto-update: $*"; }

# The report, written on the way out whatever the exit: `finish` sets what it
# says; anything that stops the script without calling it (a failed git
# command under set -e) reads as failed.
result=failed
message="stopped part-way; see journalctl -u meshradio-autoupdate"
target=""
updated_at=""
json_str() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'; }
report() {
    code=$?
    set +e
    head_now="$(git_ rev-parse HEAD 2>/dev/null)"
    if [ -z "$updated_at" ] && [ -f "$STATUS" ]; then
        updated_at="$(sed -n 's/.*"updated_at": *\([0-9][0-9]*\).*/\1/p' "$STATUS")"
    fi
    mkdir -p "$STATE" && printf '{"checked_at": %s, "result": "%s", "message": "%s", "commit": "%s", "target": "%s", "updated_at": %s}\n' \
        "$(date +%s)" "$result" "$(json_str "$message")" "$head_now" "$target" \
        "${updated_at:-null}" > "$STATUS.tmp" && mv "$STATUS.tmp" "$STATUS"
    exit "$code"
}
trap report EXIT

# finish RESULT CODE MESSAGE: record the outcome and stop. A message is
# logged unless RESULT is one of the every-ten-minutes non-events.
finish() {
    result="$1"
    message="$3"
    case "$1" in current|skipped-bad) ;; *) log "$3" ;; esac
    exit "$2"
}

# git and pip run as the clone's owner, so nothing in it ends up root-owned
# and git doesn't refuse the repository as someone else's.
as_owner() {
    if [ "$(id -un)" = "$OWNER" ]; then
        "$@"
    else
        home="$(getent passwd "$OWNER" | cut -d: -f6)"
        runuser -u "$OWNER" -- env HOME="$home" PATH="$home/.local/bin:$PATH" "$@"
    fi
}

git_() { as_owner git -C "$DIR" "$@"; }

# A venv made with `uv venv` has no pip, one made with `python -m venv`
# does; use whichever the venv has.
install() {
    if [ -x "$DIR/.venv/bin/pip" ]; then
        as_owner "$DIR/.venv/bin/pip" install --quiet -e "$DIR[$EXTRAS]"
    elif as_owner sh -c 'command -v uv' >/dev/null 2>&1; then
        as_owner uv pip install --quiet --python "$DIR/.venv/bin/python" -e "$DIR[$EXTRAS]"
    else
        log "no pip in $DIR/.venv and no uv on PATH; can't install"
        return 1
    fi
}

healthy() {
    deadline=$(( $(date +%s) + WAIT ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        if curl -fsS --max-time 5 "$HEALTH_URL" 2>/dev/null | grep -q '"ok":true'; then
            return 0
        fi
        sleep 3
    done
    return 1
}

if ! git_ fetch --quiet origin "$BRANCH" 2>/dev/null; then
    finish no-branch 0 "no $BRANCH branch on origin yet (CI makes it on the next green main); nothing to do"
fi
target="$(git_ rev-parse FETCH_HEAD)"
current="$(git_ rev-parse HEAD)"
[ "$target" = "$current" ] && finish current 0 "up to date"

if [ -f "$BAD" ] && [ "$(cat "$BAD")" = "$target" ]; then
    # already tried and rolled back; wait for the next green commit
    finish skipped-bad 0 "$target failed its health check earlier; staying on $current until the next green commit"
fi
if [ "$(git_ symbolic-ref --quiet --short HEAD || true)" != "main" ]; then
    finish blocked 1 "the clone isn't on main; leaving it alone"
fi
if ! git_ merge-base --is-ancestor "$current" "$target"; then
    finish blocked 1 "the clone has commits $BRANCH doesn't; leaving it alone"
fi

runtime="$(git_ diff --name-only "$current" "$target" -- . \
    ':(exclude)*.md' ':(exclude)tests/' ':(exclude).github/' \
    ':(exclude)render.yaml' ':(exclude)meshradio.render.toml' \
    ':(exclude)uv.lock' ':(exclude)scripts/' ':(exclude)LICENSE')"

# git refuses on its own when a local edit (or an untracked file) is in the
# way of the update, and leaves the clone as it was; edits to other files
# are carried along untouched.
clash="$(git_ diff --name-only HEAD -- $(git_ diff --name-only "$current" "$target") 2>/dev/null || true)"
if ! git_ merge --quiet --ff-only "$target" >/dev/null 2>&1; then
    if [ -n "$clash" ]; then
        finish blocked 1 "local edits to $(echo $clash) are in the way of the update; leaving the clone alone (git stash or git checkout -- those files)"
    fi
    finish blocked 1 "git couldn't fast-forward the clone to $target; try git -C $DIR merge --ff-only $target to see why"
fi
rm -f "$BAD"
updated_at="$(date +%s)"
if [ -z "$runtime" ]; then
    finish fast-forwarded 0 "fast-forwarded to $target (nothing the radio runs changed; no restart)"
fi
if git_ diff --name-only "$current" "$target" -- 'deploy/*.service' 'deploy/*.timer' | grep -q .; then
    log "note: a systemd unit in deploy/ changed; copy it to /etc/systemd/system by hand"
fi

log "updating $current -> $target"
if install && systemctl restart "$SERVICE" && healthy; then
    finish updated 0 "now on $target"
fi

log "$target didn't come up healthy; rolling back to $current"
updated_at=""
mkdir -p "$STATE"
echo "$target" > "$BAD"
git_ reset --quiet --keep "$current"
install || true
systemctl restart "$SERVICE" || true
if healthy; then
    finish rolled-back 1 "$target didn't come up healthy, so the clone is back on $current; it's healthy"
fi
finish rolled-back 1 "$target didn't come up healthy, so the clone is back on $current, but it isn't healthy either; see journalctl -u $SERVICE"
