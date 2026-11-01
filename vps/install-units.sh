#!/usr/bin/env bash
# Install the systemd units and enable the timers. Run as root; safe to re-run.
# Called by install.sh, and by section9-sync when vps/systemd changes.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit_dir=${S9_UNIT_DIR:-/etc/systemd/system}

install -m 0644 "$here"/systemd/section9-* "$unit_dir"/
systemctl daemon-reload
systemctl enable --now section9-dispatch.timer section9-routines.timer section9-sync.timer \
  section9-hq-status.timer section9-hq-crew.timer section9-spend.timer
