#!/bin/sh
# Keep the Pi on the newest commit CI has passed. Run every ten minutes by
# meshradio-autoupdate.timer; safe to run by hand (as root).
#
# CI moves the `pi-deploy` branch to each main commit whose test workflow
# went green (.github/workflows/pi-deploy.yml), so this never pulls a commit
# the suite failed. When that branch is ahead of the clone it:
#
#   1. fast-forwards the clone (as the user who owns it; a clone with local
#      edits, local commits or another branch checked out is left alone),
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
# Settings come from the environment (the unit sets them):
#   MESHRADIO_DIR          the clone                  (/home/pi/meshradio)
#   MESHRADIO_USER         who owns it                (the clone's owner)
#   MESHRADIO_SERVICE      the unit to restart        (meshradio)
#   MESHRADIO_HEALTH_URL   where to check             (http://127.0.0.1:8080/healthz)
#   MESHRADIO_EXTRAS       pip extras                 (media,hw)
#   MESHRADIO_DEPLOY_BRANCH                           (pi-deploy)
#   MESHRADIO_HEALTH_WAIT  seconds to wait for ok     (90)
#   STATE_DIRECTORY        where the bad commit is kept (systemd sets it)
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

log() { echo "auto-update: $*"; }

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
    log "no $BRANCH branch on origin yet (CI makes it on the next green main); nothing to do"
    exit 0
fi
target="$(git_ rev-parse FETCH_HEAD)"
current="$(git_ rev-parse HEAD)"
[ "$target" = "$current" ] && exit 0

if [ -f "$BAD" ] && [ "$(cat "$BAD")" = "$target" ]; then
    exit 0   # already tried and rolled back; wait for the next green commit
fi
if [ "$(git_ symbolic-ref --quiet --short HEAD || true)" != "main" ]; then
    log "the clone isn't on main; leaving it alone"
    exit 1
fi
if [ -n "$(git_ status --porcelain --untracked-files=no)" ]; then
    log "the clone has local edits; leaving it alone"
    exit 1
fi
if ! git_ merge-base --is-ancestor "$current" "$target"; then
    log "the clone has commits $BRANCH doesn't; leaving it alone"
    exit 1
fi

runtime="$(git_ diff --name-only "$current" "$target" -- . \
    ':(exclude)*.md' ':(exclude)tests/' ':(exclude).github/' \
    ':(exclude)render.yaml' ':(exclude)meshradio.render.toml' \
    ':(exclude)uv.lock' ':(exclude)scripts/' ':(exclude)LICENSE')"

git_ merge --quiet --ff-only "$target"
rm -f "$BAD"
if [ -z "$runtime" ]; then
    log "fast-forwarded to $target (nothing the radio runs changed; no restart)"
    exit 0
fi
if git_ diff --name-only "$current" "$target" -- 'deploy/*.service' 'deploy/*.timer' | grep -q .; then
    log "note: a systemd unit in deploy/ changed; copy it to /etc/systemd/system by hand"
fi

log "updating $current -> $target"
if install && systemctl restart "$SERVICE" && healthy; then
    log "now on $target"
    exit 0
fi

log "$target didn't come up healthy; rolling back to $current"
mkdir -p "$STATE"
echo "$target" > "$BAD"
git_ reset --quiet --keep "$current"
install || true
systemctl restart "$SERVICE" || true
if healthy; then
    log "rolled back to $current; it's healthy"
else
    log "rolled back to $current, but it isn't healthy either; see journalctl -u $SERVICE"
fi
exit 1
