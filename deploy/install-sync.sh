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

# No initial state: the first run applies what is checked out (compose up and
# the roster script are idempotent), so a change pulled by hand but never
# applied, such as a roster edit, still lands. An existing state file is kept.

if [ ! -r /etc/agent-manager/paperclip-board.env ]; then
  echo "warning: /etc/agent-manager/paperclip-board.env is missing; the sync can't run paperclip-apply.py without it (RUNBOOK step 7b)" >&2
fi

systemctl daemon-reload
systemctl enable --now agent-manager-sync.timer

# First sync now rather than in 2 minutes; it waits for CI if main is not green yet.
systemctl start agent-manager-sync.service || true
journalctl -u agent-manager-sync -n 20 --no-pager || true

echo
echo "Installed. Watch it with:"
echo "  systemctl list-timers agent-manager-sync.timer"
echo "  journalctl -u agent-manager-sync -n 50"
echo "Pause it with: systemctl disable --now agent-manager-sync.timer"
