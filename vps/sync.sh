#!/usr/bin/env bash
# section9-sync: fast-forward /opt/agent-manager to origin/main once CI is green on
# that commit, then re-run what changed. The VPS counterpart of deploy/vm-sync.sh,
# run by section9-sync.timer every 5 minutes. Logs to the journal. Public GitHub
# API only: no new secrets, nothing inbound.
#
# Exit: 0 = nothing to do, waiting for CI, or applied; 1 = needs a look.
set -euo pipefail

# The environment overrides are for tests; the timer sets none of them.
REPO_DIR=${S9_REPO_DIR:-/opt/agent-manager}
STATE_DIR=${S9_STATE_DIR:-/var/lib/section9}
LOCK_FILE=${S9_LOCK_FILE:-/run/section9-sync.lock}
GITHUB_API=${S9_GITHUB_API:-https://api.github.com}
GH_REPO=petterfranzen/agent-manager
MAX_ATTEMPTS=3
checks=

# Check runs that must be present and green on a commit: the job names in
# .github/workflows/ci.yml (as deploy/vm-sync.sh, plus the VPS job). Update this
# list when a CI job is added, renamed or removed, or the sync waits for ever.
REQUIRED_CHECKS=(
  "Secret scan"
  "Compose and config"
  "Sandbox image smoke test"
  "Roster script tests"
  "Deploy scripts"
  "VPS stack"
)

log() { echo "section9-sync: $*"; }
err() { echo "section9-sync: $*" >&2; }

# Prints why a commit is not ready and returns 1, or returns 0 when every
# required check run is completed with conclusion "success" (as vm-sync's ci_verdict).
ci_verdict() { # ci_verdict <check-runs.json>
  python3 -I - "$1" "${REQUIRED_CHECKS[@]}" <<'PY'
import json
import sys

path, required = sys.argv[1], sys.argv[2:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    runs = data["check_runs"]
    total = data.get("total_count", len(runs))
except (OSError, ValueError, KeyError, TypeError, AttributeError):
    print("unreadable check-runs reply")
    sys.exit(1)
if total > len(runs):
    print(f"{total} check runs, more than one page")
    sys.exit(1)
names = {r.get("name") for r in runs}
missing = [n for n in required if n not in names]
not_green = [f"{r.get('name')} ({r.get('conclusion') or r.get('status')})"
             for r in runs if r.get("status") != "completed" or r.get("conclusion") != "success"]
problems = []
if missing:
    problems.append("missing: " + ", ".join(missing))
if not_green:
    problems.append("not green: " + ", ".join(not_green))
if problems:
    print("; ".join(problems))
    sys.exit(1)
PY
}

# Re-run what changed between two commits (every step needs || return 1: set -e is off in an if): $1 = the last applied commit, $2 = the new one.
apply() {
  local changed
  changed=$(git diff --name-only "$1" "$2")
  if grep -qE '^(vps/compose\.yaml|sandbox/|hermes/)' <<<"$changed"; then
    log "compose up (compose, sandbox or Hermes config changed)"
    install -d -m 0755 "$STATE_DIR/hq" || return 1
    S9_STATE_DIR="$STATE_DIR" "$REPO_DIR/vps/dashboard-prep.sh" || return 1
    docker compose -f "$REPO_DIR/vps/compose.yaml" up -d --build --remove-orphans || return 1
    # Leftover Impeccable skill on the Hermes volume (see install.sh); idempotent.
    docker compose -f "$REPO_DIR/vps/compose.yaml" exec -T hermes rm -rf /opt/data/skills/impeccable || return 1
  fi
  if grep -qE '^vps/(systemd/|install-units\.sh)' <<<"$changed"; then
    log "re-installing systemd units"
    "$REPO_DIR/vps/install-units.sh" || return 1
  fi
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0
  cd "$REPO_DIR"
  if [ -n "$(git status --porcelain)" ]; then
    err "refusing to sync: $REPO_DIR has local changes; commit or discard them"
    exit 1
  fi
  git fetch --quiet origin main || { err "git fetch failed"; exit 1; }
  local target applied tries
  target=$(git rev-parse origin/main)
  # The commit last applied; a first run assumes the checkout is what install.sh set up.
  applied=$(cat "$STATE_DIR/sync-applied" 2>/dev/null || git rev-parse HEAD)
  [ "$target" != "$applied" ] || exit 0
  git merge-base --is-ancestor "$applied" "$target" || { err "origin/main is not a descendant of ${applied:0:7} (force-pushed?)"; exit 1; }

  local why
  checks=$(mktemp)
  trap 'rm -f "$checks"' EXIT
  curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" -H "User-Agent: section9-sync" \
    "$GITHUB_API/repos/$GH_REPO/commits/$target/check-runs?per_page=100" >"$checks" || { err "could not read CI status"; exit 1; }
  if ! why=$(ci_verdict "$checks"); then
    log "waiting for CI on ${target:0:7}: $why"
    exit 0
  fi

  tries=0
  if [ "$(cut -d' ' -f1 "$STATE_DIR/sync-failed" 2>/dev/null)" = "$target" ]; then tries=$(cut -d' ' -f2 "$STATE_DIR/sync-failed"); fi
  if [ "$tries" -ge "$MAX_ATTEMPTS" ]; then
    err "${target:0:7} failed $tries times; not retrying until main moves"
    exit 1
  fi

  # Remember where we started, so a failed apply is retried from here (HEAD moves with the merge).
  install -d -m 0755 "$STATE_DIR"
  [ -e "$STATE_DIR/sync-applied" ] || echo "$applied" >"$STATE_DIR/sync-applied"
  git merge --ff-only --quiet "$target"
  if apply "$applied" "$target"; then
    echo "$target" >"$STATE_DIR/sync-applied"
    rm -f "$STATE_DIR/sync-failed"
    log "applied ${target:0:7} \"$(git log -1 --format=%s "$target")\""
  else
    echo "$target $((tries + 1))" >"$STATE_DIR/sync-failed"
    err "FAILED applying ${target:0:7} (attempt $((tries + 1)) of $MAX_ATTEMPTS); the checkout is already at it"
    exit 1
  fi
}

# One call, then exit: bash must not read on after git replaces this file.
main "$@"
exit 0
