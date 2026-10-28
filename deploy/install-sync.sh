#!/usr/bin/env bash
# Install the pull-based deploy on the VM. Run once as root; safe to re-run.
# See docs/RUNBOOK.md, step 10.
#   bash /opt/agent-manager/deploy/install-sync.sh
# Re-run it after a change to deploy/ to install the new script or units: the
# sync applies the repo, but it does not replace its own installed copy.
set -euo pipefail

REPO=/opt/agent-manager
STATE_DIR=/var/lib/agent-manager
STATE_FILE=$STATE_DIR/sync-state.json
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "error: run as root" >&2; exit 1; }
[ "$(cd "$HERE/.." && pwd)" = "$REPO" ] || { echo "error: the sync expects the repo at $REPO" >&2; exit 1; }
for cmd in git curl python3 flock docker systemctl; do
  command -v "$cmd" >/dev/null || { echo "error: $cmd is not installed" >&2; exit 1; }
done
docker compose version >/dev/null || { echo "error: docker compose is not installed" >&2; exit 1; }

install -m 0755 -o root -g root "$HERE/vm-sync.sh" /usr/local/sbin/agent-manager-sync
install -m 0644 -o root -g root "$HERE/agent-manager-sync.service" /etc/systemd/system/agent-manager-sync.service
install -m 0644 -o root -g root "$HERE/agent-manager-sync.timer" /etc/systemd/system/agent-manager-sync.timer

mkdir -p "$STATE_DIR"
chmod 0700 "$STATE_DIR"

# Start from what is checked out now, so the first run doesn't re-apply what
# is already running. A state file that exists is kept.
if [ ! -e "$STATE_FILE" ]; then
  sha=$(git -C "$REPO" rev-parse HEAD)
  python3 -I - "$STATE_FILE" "$sha" <<'PY'
import json
import os
import sys
from datetime import datetime, timezone

path, sha = sys.argv[1:3]
state = {
    "appliedSha": sha,
    "lastAttemptSha": sha,
    "attempts": 0,
    "result": "success",
    "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
}
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", encoding="utf-8") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
PY
  echo "initial state: appliedSha=${sha:0:7}"
fi

if [ ! -r /etc/agent-manager/paperclip-board.env ]; then
  echo "warning: /etc/agent-manager/paperclip-board.env is missing; the sync can't run paperclip-apply.py without it (RUNBOOK step 7b)" >&2
fi

systemctl daemon-reload
systemctl enable --now agent-manager-sync.timer

echo
echo "Installed. Watch it with:"
echo "  systemctl list-timers agent-manager-sync.timer"
echo "  journalctl -u agent-manager-sync -n 50"
echo "Pause it with: systemctl disable --now agent-manager-sync.timer"
