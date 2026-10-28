#!/usr/bin/env bash
# agent-manager-sync: the VM pulls main and applies a commit once all of its
# CI checks have passed. Installed as /usr/local/sbin/agent-manager-sync by
# deploy/install-sync.sh and run as root by agent-manager-sync.timer.
# See docs/RUNBOOK.md, step 10.
#
# No inbound access and no new secrets: it only reads the public GitHub API
# and the repo, and reuses the board key the roster script already uses.
#
# Exit codes: 0 = nothing to do, waiting for CI, or applied; 1 = needs a look
# (dirty repo, force-pushed main, retry limit, or a failed apply).
set -euo pipefail

# The environment overrides are for tests; the timer sets none of them.
REPO_DIR=${AM_REPO_DIR:-/opt/agent-manager}
STATE_DIR=${AM_STATE_DIR:-/var/lib/agent-manager}
LOCK_FILE=${AM_LOCK_FILE:-/run/agent-manager-sync.lock}
SECRETS_DIR=${AM_SECRETS_DIR:-/etc/agent-manager}
GITHUB_API=${AM_GITHUB_API:-https://api.github.com}
PAPERCLIP_URL=${AM_PAPERCLIP_URL:-http://127.0.0.1:3100}
GH_REPO=petterfranzen/agent-manager
STATE_FILE=$STATE_DIR/sync-state.json

# Check runs that must be present and green on a commit before it is applied.
# These are the job names in .github/workflows/ci.yml: update this list when
# a CI job is added, renamed or removed, or the sync waits for ever.
REQUIRED_CHECKS=(
  "Secret scan"
  "Compose and config"
  "Sandbox image smoke test"
  "Roster script tests"
  "Deploy scripts"
)

log() { echo "agent-manager-sync: $*"; }
err() { echo "agent-manager-sync: $*" >&2; }

# Keys never go to the journal or to Paperclip.
redact() { sed -E 's/(pcp_board_|sk-or-|github_pat_)[A-Za-z0-9_-]*/[redacted]/g'; }

state_get() { # state_get <key> <default>
  python3 -I - "$STATE_FILE" "$1" "$2" <<'EOF'
import json
import sys

path, key, default = sys.argv[1:4]
try:
    with open(path, encoding="utf-8") as f:
        value = json.load(f).get(key, default)
except (OSError, ValueError, AttributeError):
    value = default
print(value)
EOF
}

state_write() { # state_write <appliedSha> <lastAttemptSha> <attempts> <result> <failedStep>
  python3 -I - "$STATE_FILE" "$@" <<'EOF'
import json
import os
import sys
from datetime import datetime, timezone

path, applied, last, attempts, result, step = sys.argv[1:7]
state = {
    "appliedSha": applied,
    "lastAttemptSha": last,
    "attempts": int(attempts),
    "result": result,
    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
if step:
    state["failedStep"] = step
tmp = path + ".tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
EOF
}

# Prints why a commit is not ready and returns 1, or returns 0 when every
# required check run is completed with conclusion "success".
ci_verdict() { # ci_verdict <check-runs.json>
  python3 -I - "$1" "${REQUIRED_CHECKS[@]}" <<'EOF'
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
not_green = [
    f"{r.get('name')} ({r.get('conclusion') or r.get('status')})"
    for r in runs
    if r.get("status") != "completed" or r.get("conclusion") != "success"
]
problems = []
if missing:
    problems.append("missing: " + ", ".join(missing))
if not_green:
    problems.append("not green: " + ", ".join(not_green))
if problems:
    print("; ".join(problems))
    sys.exit(1)
EOF
}

wait_for_paperclip() {
  local deadline=$((SECONDS + 120))
  until curl -fsS --max-time 5 "$PAPERCLIP_URL/api/health" >/dev/null 2>&1; do
    if [ "$SECONDS" -ge "$deadline" ]; then
      echo "Paperclip did not answer $PAPERCLIP_URL/api/health within 120 s"
      return 1
    fi
    sleep 2
  done
}

# Best effort: one comment per deploy on the open "Deploys" issue in Paperclip.
# Never prints the board key. A failure here is logged and ignored.
report() { # report <result> <failedStep> <seconds> <text file>
  python3 -I - "$TARGET" "$SUBJECT" "$1" "$2" "$3" "$4" \
    "$SECRETS_DIR/paperclip-board.env" "$PAPERCLIP_URL" <<'EOF'
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sha, subject, result, step, seconds, text_file, env_file, base = sys.argv[1:9]
COMPANY = "Section 9"
TITLE = "Deploys"
DESCRIPTION = "The VM posts one comment per deploy here."
OPEN = "backlog,todo,in_progress,in_review,blocked"
MAX_LINES = 40
KEYS = re.compile(r"(pcp_board_|sk-or-|github_pat_)[A-Za-z0-9_-]*")
secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    """Last NAME=VALUE line in an env file; quotes stripped (as paperclip-apply.py)."""
    value = None
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, val = line.partition("=")
            if sep and key.strip() == name:
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                value = val
    if not value:
        raise RuntimeError(f"PAPERCLIP_BOARD_KEY is missing or empty in {path}")
    return value


def call(method, path, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = redact(e.read().decode("utf-8", "replace"))[:300]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise RuntimeError(f"{method} {path} -> no response: {redact(str(getattr(e, 'reason', e)))}") from None
    return json.loads(raw) if raw.strip() else None


def q(value):
    return urllib.parse.quote(str(value), safe="")


try:
    host = urllib.parse.urlsplit(base)
    if host.scheme != "http" or host.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise RuntimeError("refusing to send the board key to a non-loopback host")
    key = read_env_value(env_file, "PAPERCLIP_BOARD_KEY")
    secrets.append(key)

    with open(text_file, encoding="utf-8", errors="replace") as f:
        lines = f.read().splitlines()
    ok = result == "success"
    if ok:
        lines = [ln for ln in lines if not ln.startswith("ok:") and ln.strip()]
        kept = lines[:MAX_LINES]
        more = len(lines) - len(kept)
    else:
        kept = lines[-MAX_LINES:]
        more = len(lines) - len(kept)
    kept = [redact(ln)[:300] for ln in kept]

    out = [f"Deploy `{sha[:7]}`: {redact(subject)}", ""]
    out.append("Result: success" if ok else f"Result: failure at step `{step}`")
    out.append(f"Duration: {seconds} s")
    out.append("")
    if ok:
        out.append("Paperclip changes:" if kept else "No Paperclip changes.")
    else:
        out.append("End of the error output:")
    if kept:
        out.append("```text")
        if more > 0 and ok:
            kept.append(f"... and {more} more line(s)")
        elif more > 0:
            kept.insert(0, f"... {more} earlier line(s) left out")
        out.extend(kept)
        out.append("```")

    companies = call("GET", "/api/companies") or []
    found = [c for c in companies if c.get("name") == COMPANY and c.get("status") != "archived"]
    if len(found) != 1:
        raise RuntimeError(f"expected one company named '{COMPANY}', found {len(found)}")
    cid = found[0]["id"]

    issues = call("GET", f"/api/companies/{q(cid)}/issues?status={q(OPEN)}") or []
    issue = next((i for i in issues if i.get("title") == TITLE), None)
    if issue is None:
        # allowDuplicate: otherwise a recent closed "Deploys" issue could be returned instead.
        issue = call("POST", f"/api/companies/{q(cid)}/issues", {
            "title": TITLE,
            "description": DESCRIPTION,
            "status": "backlog",
            "priority": "low",
            "allowDuplicate": True,
        })
    call("POST", f"/api/issues/{q(issue['id'])}/comments", {"body": "\n".join(out)})
except Exception as e:  # best effort by design
    print(f"report to Paperclip failed: {redact(str(e))}", file=sys.stderr)
    sys.exit(1)
EOF
}

# Only one sync at a time (a deploy can outlast the 5 minute timer).
exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

cd "$REPO_DIR"
if [ -n "$(git status --porcelain)" ]; then
  err "refusing to sync: $REPO_DIR has local changes (hand edits would be overwritten); commit or discard them"
  exit 1
fi

git fetch --quiet origin main || { err "git fetch failed"; exit 1; }
TARGET=$(git rev-parse origin/main)

APPLIED=$(state_get appliedSha "")
if [ "$TARGET" = "$APPLIED" ]; then
  exit 0
fi

LAST=$(state_get lastAttemptSha "")
ATTEMPTS=$(state_get attempts 0)
RESULT=$(state_get result "")
[[ $ATTEMPTS =~ ^[0-9]+$ ]] || ATTEMPTS=0
if [ "$LAST" = "$TARGET" ] && [ "$RESULT" = failure ] && [ "$ATTEMPTS" -ge 3 ]; then
  err "giving up on ${TARGET:0:7} after $ATTEMPTS failed attempts (step: $(state_get failedStep "?")); a new commit on main resets this"
  exit 1
fi

if ! git merge-base --is-ancestor HEAD "$TARGET"; then
  err "origin/main (${TARGET:0:7}) does not contain the deployed HEAD ($(git rev-parse --short HEAD)); was main force-pushed? Not applying"
  exit 1
fi

CHECKS_JSON=$(mktemp)
OUT=$(mktemp)
APPLY_OUT=$(mktemp)
trap 'rm -f "$CHECKS_JSON" "$OUT" "$APPLY_OUT"' EXIT

curl -fsS --max-time 20 -H 'Accept: application/vnd.github+json' -o "$CHECKS_JSON" \
  "$GITHUB_API/repos/$GH_REPO/commits/$TARGET/check-runs?per_page=100" \
  || { err "cannot read the check runs of ${TARGET:0:7}"; exit 1; }
if ! WHY=$(ci_verdict "$CHECKS_JSON"); then
  log "waiting for CI on ${TARGET:0:7}: $WHY"
  exit 0
fi

SUBJECT=$(git log -1 --format=%s "$TARGET")
START=$SECONDS
# Tries on this commit, counting the one about to run.
if [ "$LAST" = "$TARGET" ]; then NEXT_ATTEMPT=$((ATTEMPTS + 1)); else NEXT_ATTEMPT=1; fi
STEP=""

# No rollback: a failed step stays as it is for a human to look at.
# Every step needs "|| return 1": set -e is off inside a function in an "if".
apply() {
  STEP="snapshot"
  OLD_PAPERCLIP=$(docker compose ps -q paperclip) || return 1
  STEP="git merge"
  git merge --ff-only --quiet "$TARGET" || return 1
  STEP="compose up"
  docker compose up -d --build --remove-orphans || return 1
  STEP="restart hermes"
  NEW_PAPERCLIP=$(docker compose ps -q paperclip) || return 1
  if [ "$OLD_PAPERCLIP" != "$NEW_PAPERCLIP" ]; then
    # Hermes shares paperclip's network namespace, so it needs a restart
    # whenever paperclip's container was replaced.
    docker compose restart hermes || return 1
  fi
  STEP="paperclip health"
  wait_for_paperclip || return 1
  STEP="paperclip-apply"
  python3 scripts/paperclip-apply.py >"$APPLY_OUT" 2>&1 || return 1
  STEP=""
}

if apply >"$OUT" 2>&1; then
  DURATION=$((SECONDS - START))
  state_write "$TARGET" "$TARGET" "$NEXT_ATTEMPT" success ""
  log "applied ${TARGET:0:7} \"$SUBJECT\" in ${DURATION}s; paperclip-apply: $(tail -n 1 "$APPLY_OUT")"
  grep -v '^ok:' "$APPLY_OUT" | redact | sed 's/^/agent-manager-sync:   /' || true
  report success "" "$DURATION" "$APPLY_OUT" || log "could not post to Paperclip (ignored)"
  exit 0
fi

DURATION=$((SECONDS - START))
FAILED_STEP=$STEP
state_write "$APPLIED" "$TARGET" "$NEXT_ATTEMPT" failure "$FAILED_STEP"
# The failing step's output: paperclip-apply has its own file, the rest share OUT.
TAIL_FILE=$OUT
if [ "$FAILED_STEP" = "paperclip-apply" ]; then
  TAIL_FILE=$APPLY_OUT
fi
tail -n 30 "$TAIL_FILE" | redact >"$TAIL_FILE.tail"
mv "$TAIL_FILE.tail" "$TAIL_FILE"
err "FAILED ${TARGET:0:7} \"$SUBJECT\" at step '$FAILED_STEP' after ${DURATION}s; nothing was rolled back"
sed 's/^/agent-manager-sync:   /' "$TAIL_FILE" >&2
report failure "$FAILED_STEP" "$DURATION" "$TAIL_FILE" || log "could not post to Paperclip (ignored)"
exit 1
