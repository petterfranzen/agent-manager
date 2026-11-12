#!/usr/bin/env bash
# section9-sync: fast-forward /opt/agent-manager to origin/main once CI is green on
# that commit, it has soaked for 30 min and the few protected paths it changes are
# pinned, then re-run what changed. Everything else applies by itself. The VPS counterpart of deploy/vm-sync.sh, run by section9-sync.timer
# every 5 minutes. Logs to the journal. GitHub API only: no new secrets.
#
# Exit: 0 = nothing to do, waiting for CI, or applied; 1 = needs a look.
set -euo pipefail

# The environment overrides are for tests; the timer sets none of them.
REPO_DIR=${S9_REPO_DIR:-/opt/agent-manager}
STATE_DIR=${S9_STATE_DIR:-/var/lib/section9}
SECRETS_DIR=${S9_SECRETS_DIR:-/etc/section9}
PINS_FILE=${S9_PINS_FILE:-/etc/section9/pins.toml}
DISPATCH_PY=${S9_DISPATCH_PY:-$REPO_DIR/vps/dispatch.py}
LOCK_FILE=${S9_LOCK_FILE:-/run/section9-sync.lock}
SYNC_CONFIG=${S9_SYNC_CONFIG:-/etc/section9/sync.toml}
GITHUB_API=${S9_GITHUB_API:-https://api.github.com}
GH_REPO=petterfranzen/agent-manager
MAX_ATTEMPTS=3
self_changed=0
checks=

# The protected set: what could widen what a merge to main can do as root, or weaken the
# gate itself. Any other change applies on its own (CI green, fast-forward, soaked). A
# commit may change a protected path only when PINS_FILE--root-only, outside the repo, so
# the commit cannot update it--already carries that file's new sha256; otherwise the sync
# stops until a human runs `sync.sh --accept`. A prefix ending in "/" is a directory; any
# path with "sudoers" in its name is protected too. /etc/section9/sync.toml can ADD
# prefixes (protect = ["..."]) and set soak_minutes; it cannot remove these.
#   sync.sh, install.sh, install-units.sh: the pins/trust logic and what installs the units
#   systemd/section9-sync.*: the units that run the sync
#   .github/workflows/: CI is the green light the sync trusts
PROTECTED=(vps/sync.sh vps/install.sh vps/install-units.sh vps/systemd/section9-sync. .github/workflows/)
SOAK_DEFAULT=30       # minutes a commit must have been on main before it applies

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

# Keep the open "Sync" issue in section9-hq current: what the VPS did with itself.
# Best effort: a refusal must still stop the sync when the board cannot be reached.
note() { # note <ok|blocked> <message>
  local hf="$STATE_DIR/sync-health.txt"
  install -d -m 0755 "$STATE_DIR" 2>/dev/null || true
  health >"$hf" 2>/dev/null || true
  printf '%s\n' "$1" >"$STATE_DIR/sync-note.state" 2>/dev/null || true
  printf '%s\n' "$2" >"$STATE_DIR/sync-note.msg" 2>/dev/null || true
  python3 -I "$DISPATCH_PY" note --state "$1" --message "$2" --health-file "$hf" \
    --secrets-dir "$SECRETS_DIR" --github "$GITHUB_API" >&2 \
    || { err "could not update the Sync issue"; return 0; }
  # What was posted, so an idle tick only rewrites the issue when something changed (or hourly).
  { printf '%s\n%s\n' "$1" "$2"; sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+ UTC, [0-9]+ min ago//' "$hf"; } \
    | sha256sum | cut -d' ' -f1 >"$STATE_DIR/sync-note.hash" 2>/dev/null || true
  date +%s >"$STATE_DIR/sync-note.at" 2>/dev/null || true
}

# On a tick with nothing to apply: keep the Sync issue's timer health current. Posts only when
# the message or the health (ignoring times) changed, or the last post is over an hour old.
idle_note() { # idle_note <message>
  local hf cur at
  hf=$(mktemp)
  health >"$hf" 2>/dev/null || true
  cur=$({ printf 'ok\n%s\n' "$1"; sed -E 's/[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9:]+ UTC, [0-9]+ min ago//' "$hf"; } | sha256sum | cut -d' ' -f1)
  at=$(cat "$STATE_DIR/sync-note.at" 2>/dev/null || echo 0)
  rm -f "$hf"
  if [ "$cur" != "$(cat "$STATE_DIR/sync-note.hash" 2>/dev/null || true)" ] || [ $(($(date +%s) - at)) -ge 3600 ]; then
    note ok "$1"
  fi
}

# Reads the optional root-only SYNC_CONFIG. Prints "soak=<minutes>" and one "protect=<prefix>"
# line per extra protected prefix, or why it is unusable (returns 1: the sync fails closed).
sync_config() {
  python3 -I - "$SYNC_CONFIG" "$SOAK_DEFAULT" <<'PY'
import sys
import tomllib

path, soak = sys.argv[1], int(sys.argv[2])
try:
    with open(path, encoding="utf-8") as f:
        data = tomllib.loads(f.read())
except FileNotFoundError:
    data = {}
except (OSError, ValueError) as e:
    print(f"cannot read {path}: {type(e).__name__}")
    sys.exit(1)
soak = data.get("soak_minutes", soak)
extra = data.get("protect", [])
if not isinstance(soak, int) or isinstance(soak, bool) or not 0 <= soak <= 1440:
    print(f"{path}: soak_minutes must be a whole number of minutes, 0 to 1440")
    sys.exit(1)
if not isinstance(extra, list) or not all(isinstance(x, str) and x and not x.startswith("/") and ".." not in x.split("/") and "\n" not in x for x in extra):
    print(f"{path}: protect must be a list of repo-relative path prefixes")
    sys.exit(1)
print(f"soak={soak}")
for x in extra:
    print(f"protect={x}")
PY
}

# Timer health for the Sync issue: is each timer active, when did it last fire, how did the
# service end, and the last error-looking journal line. Plain text, never the private files.
health() {
  local u active last ts now age result err flag
  now=$(date +%s)
  for u in section9-routines section9-dispatch; do
    active=$(systemctl is-active "$u.timer" 2>/dev/null || true)
    last=$(systemctl show "$u.timer" -p LastTriggerUSec --value 2>/dev/null || true)
    result=$(systemctl show "$u.service" -p Result --value 2>/dev/null || true)
    ts=$(date -d "$last" +%s 2>/dev/null || echo 0)
    flag=
    if [ "$ts" -gt 0 ]; then
      age=$(((now - ts) / 60))
      last="$(date -u -d "@$ts" '+%Y-%m-%d %H:%M') UTC, $age min ago"
      [ "$age" -le 30 ] || flag=" STALLED (no run for over 30 min)"
    else
      last="no trigger since boot"
      flag=" STALLED (never triggered)"
    fi
    [ "$active" = active ] || flag=" STALLED (timer is not active)"
    err=$(journalctl -u "$u.service" -n 200 --no-pager -o cat 2>/dev/null | grep -iE 'error|fail|refus|cannot|traceback' | tail -1 \
      | sed -E 's/(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*/[redacted]/g' | tr -d '`' | cut -c1-200 || true)
    echo "- \`$u.timer\`: ${active:-unknown}; last trigger $last; last service result ${result:-unknown}; last error line: ${err:-none}.$flag"
  done
}

# Prints why a commit must not be applied and returns 1, or prints nothing and
# returns 0 when every protected path it changes is pinned to its new content in
# PINS_FILE. Reads the commit's own blobs: the checkout is untouched.
pins_verdict() { # pins_verdict <from> <to>
  python3 -I - "$1" "$2" "$PINS_FILE" "$REPO_DIR" "${PROTECTED[@]}" <<'PY'
import hashlib
import subprocess
import sys
import tomllib

base, target, pins_file, repo, *prefixes = sys.argv[1:]
try:
    with open(pins_file, encoding="utf-8") as f:
        pins = tomllib.loads(f.read()).get("pins", {})
except OSError:               # no pins yet: every protected change needs a look
    pins = {}
except ValueError as e:
    print(f"cannot read {pins_file}: {e}")
    sys.exit(1)
changed = subprocess.run(["git", "diff", "--name-only", base, target], cwd=repo,
                         capture_output=True, text=True, check=True).stdout.split()
problems = []
for path in changed:
    if not (path.startswith(tuple(prefixes)) or "sudoers" in path.lower()):
        continue
    blob = subprocess.run(["git", "cat-file", "blob", f"{target}:{path}"], cwd=repo, capture_output=True)
    if blob.returncode:       # deleted: nothing of it is left to run here
        continue
    if pins.get(path) != hashlib.sha256(blob.stdout).hexdigest():
        problems.append(path + (" is new" if path not in pins else " changed"))
if problems:
    print(f"{pins_file} does not pin " + ", ".join(problems))
    sys.exit(1)
PY
}

# Applies SYNC_CONFIG: sets SOAK (minutes) and appends the extra protected prefixes. Fails closed.
load_config() {
  local cfg line
  cfg=$(sync_config) || { err "$cfg"; return 1; }
  SOAK=$(sed -n 's/^soak=//p' <<<"$cfg")
  [ -z "${S9_SOAK_MINUTES:-}" ] || SOAK=$S9_SOAK_MINUTES       # tests only; the timer sets none
  while IFS= read -r line; do PROTECTED+=("$line"); done < <(sed -n 's/^protect=//p' <<<"$cfg")
}

# Pin the protected paths as a commit has them, so the sync will accept it. Root-only
# output, written by hand after reading the change. Usage: sync.sh --accept [commit]
accept() { # accept [commit]  (default origin/main)
  cd "$REPO_DIR"
  if [ -z "${1:-}" ]; then
    git fetch --quiet origin main || { err "git fetch failed"; exit 1; }
    set -- origin/main
  fi
  local tree
  load_config || exit 1
  tree=$(git rev-parse "$1")
  [ -z "$(git status --porcelain)" ] || { err "refusing to pin: $REPO_DIR has local changes"; exit 1; }
  install -d -m 0755 "$(dirname "$PINS_FILE")"
  python3 -I - "$tree" "$PINS_FILE" "$REPO_DIR" "${PROTECTED[@]}" <<'PY'
import hashlib
import os
import subprocess
import sys
import tomllib

tree, pins_file, repo, *prefixes = sys.argv[1:]
try:
    with open(pins_file, encoding="utf-8") as f:
        pins = tomllib.loads(f.read()).get("pins", {})
except OSError:
    pins = {}
except ValueError as e:
    print(f"cannot read {pins_file}: {e}")
    sys.exit(1)
names = subprocess.run(["git", "ls-tree", "-r", "--name-only", tree], cwd=repo,
                       capture_output=True, text=True, check=True).stdout.split()
for path in names:
    if path.startswith(tuple(prefixes)) or "sudoers" in path.lower():
        blob = subprocess.run(["git", "cat-file", "blob", f"{tree}:{path}"], cwd=repo,
                              capture_output=True, check=True).stdout
        pins[path] = hashlib.sha256(blob).hexdigest()
with open(pins_file, "w", encoding="utf-8") as f:
    f.write("# Root-only (0600). The protected paths of agent-manager as the reviewer of this\n"
            "# host read them. Written by `sync.sh --accept`; the sync applies a protected change\n"
            "# only when its sha256 is here. Delete a line to look again at that file.\n[pins]\n")
    for path, digest in sorted(pins.items()):
        f.write(f'"{path}" = "{digest}"\n')
os.chmod(pins_file, 0o600)
print(f"{len(pins)} protected paths pinned in {pins_file} at {tree[:7]}")
PY
}

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
  if grep -qx 'vps/sync\.sh' <<<"$changed"; then
    self_changed=1
  fi
  if grep -qE '^(vps/compose\.yaml|sandbox/|hermes/)' <<<"$changed"; then
    log "compose up (compose, sandbox or Hermes config changed)"
    install -d -m 0755 "$STATE_DIR/hq" || return 1
    S9_STATE_DIR="$STATE_DIR" "$REPO_DIR/vps/dashboard-prep.sh" || return 1
    docker compose -f "$REPO_DIR/vps/compose.yaml" up -d --build --remove-orphans || return 1
    # Leftover Impeccable skill on the Hermes volume (see install.sh); idempotent.
    docker compose -f "$REPO_DIR/vps/compose.yaml" exec -T hermes rm -rf /opt/data/skills/impeccable || return 1
  fi
  if grep -qE '^vps/(apt/|systemd/|install-units\.sh)' <<<"$changed"; then
    log "re-installing systemd units and the apt drop-in"
    "$REPO_DIR/vps/install-units.sh" || return 1
  fi
}

main() {
  exec 9>"$LOCK_FILE"
  flock -n 9 || exit 0
  cd "$REPO_DIR"
  if ! load_config; then
    note blocked "Refusing to sync: $SYNC_CONFIG is unusable ($(sed -n '1p' <<<"$(sync_config 2>&1 || true)")), so nothing is applied. Fix or remove that file."
    exit 1
  fi
  if [ -n "$(git status --porcelain)" ]; then
    err "refusing to sync: $REPO_DIR has local changes; commit or discard them"
    note blocked "Refusing to sync: $REPO_DIR has local changes, so nothing is applied. A hand edit there stops every tick until it is committed or discarded. \`git status --porcelain\` says: $(git status --porcelain | head -20 | tr '\n' '; ')"
    exit 1
  fi

  # A commit that changed this file ran the old steps (the new file is on disk, the
  # running copy is the old one). The new steps run now, on the same range.
  if [ -n "${S9_REPLAY:-}" ]; then
    # shellcheck disable=SC2086
    set -- $S9_REPLAY
    if apply "$1" "$2"; then
      echo "$2" >"$STATE_DIR/sync-applied"
      rm -f "$STATE_DIR/sync-failed"
      log "applied ${2:0:7} with the new sync.sh"
      note ok "Applied ${2:0:7} again with the sync.sh it brought, so its own new steps ran."
    else
      err "FAILED replaying ${2:0:7} with the new sync.sh"
      note blocked "FAILED applying ${2:0:7} with the sync.sh that commit brought. The checkout is at it; look at the journal."
      exit 1
    fi
    return
  fi

  git fetch --quiet origin main || { err "git fetch failed"; exit 1; }
  local target applied tries
  target=$(git rev-parse origin/main)
  # The commit last applied; a first run assumes the checkout is what install.sh set up.
  applied=$(cat "$STATE_DIR/sync-applied" 2>/dev/null || git rev-parse HEAD)
  if [ "$target" = "$applied" ]; then
    idle_note "Up to date at ${applied:0:7}."
    exit 0
  fi
  git merge-base --is-ancestor "$applied" "$target" || { err "origin/main is not a descendant of ${applied:0:7} (force-pushed?)"; exit 1; }

  local why
  checks=$(mktemp)
  trap 'rm -f "$checks"' EXIT
  curl -fsS --max-time 20 -H "Accept: application/vnd.github+json" -H "User-Agent: section9-sync" \
    "$GITHUB_API/repos/$GH_REPO/commits/$target/check-runs?per_page=100" >"$checks" || { err "could not read CI status"; exit 1; }
  if ! why=$(ci_verdict "$checks"); then
    log "waiting for CI on ${target:0:7}: $why"
    idle_note "Waiting for CI to go green on ${target:0:7}; it applies after that and a $SOAK min soak."
    exit 0
  fi

  # The checks are produced by the commit's own workflow file, so they are only a
  # gate once that file is the one a human pinned (.github/workflows/ is protected).
  # The same goes for the sync's own files and anything else in the protected set.
  if ! why=$(pins_verdict "$applied" "$target"); then
    err "${target:0:7} is green but not pinned: $why"
    note blocked "CI is green on ${target:0:7} but it changes a protected file, so the sync will not apply it: $why. Read the change, then \`sudo /opt/agent-manager/vps/sync.sh --accept\` on the VPS to pin it."
    exit 1
  fi

  # Soak: only the tip of main ever applies, so a bad merge that is reverted (or fixed)
  # within the soak never lands. The committer time of a squash merge is the merge time.
  local age
  age=$(( ($(date +%s) - $(git log -1 --format=%ct "$target")) / 60 ))
  if [ "$age" -lt "$SOAK" ]; then
    log "${target:0:7} is green but only $age min old; applies after a $SOAK min soak"
    idle_note "${target:0:7} is green and soaking: it applies once it has been on main for $SOAK min (a revert in that time stops it)."
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
    if [ "$self_changed" = 1 ]; then
      log "sync.sh changed in ${target:0:7}; re-running it so its own new steps run too"
      exec 9>&-                                     # drop the lock: the re-run takes it again
      S9_REPLAY="$applied $target" exec "$REPO_DIR/vps/sync.sh"
    fi
    echo "$target" >"$STATE_DIR/sync-applied"
    rm -f "$STATE_DIR/sync-failed"
    log "applied ${target:0:7} \"$(git log -1 --format=%s "$target")\""
    note ok "Applied ${target:0:7}: $(git log -1 --format=%s "$target")."
  else
    echo "$target $((tries + 1))" >"$STATE_DIR/sync-failed"
    err "FAILED applying ${target:0:7} (attempt $((tries + 1)) of $MAX_ATTEMPTS); the checkout is already at it"
    note blocked "FAILED applying ${target:0:7} (attempt $((tries + 1)) of $MAX_ATTEMPTS); the checkout is already at it. Look at the journal."
    exit 1
  fi
}

# One call, then exit: bash must not read on after git replaces this file.
if [ "${1:-}" = "--accept" ]; then
  shift
  accept "$@"
  exit 0
fi
main "$@"
exit 0
