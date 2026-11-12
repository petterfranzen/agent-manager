#!/usr/bin/env bash
# Install the systemd units and the apt drop-in, and enable the timers. Run as
# root; safe to re-run. Called by install.sh, and by section9-sync when vps/systemd
# (or vps/apt) changes.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
unit_dir=${S9_UNIT_DIR:-/etc/systemd/system}

install -m 0644 "$here"/systemd/section9-* "$unit_dir"/
# Keep unattended-upgrades off cloudflared: vps/cloudflared-patch.sh owns it, with
# the 24 h delay (docs/DECISIONS.md, Principles).
install -d -m 0755 /etc/apt/apt.conf.d
install -m 0644 "$here"/apt/52section9-cloudflared /etc/apt/apt.conf.d/
systemctl daemon-reload
systemctl enable --now section9-dispatch.timer section9-dispatch-lane1.timer section9-routines.timer \
  section9-sync.timer section9-health.timer \
  section9-hq-status.timer section9-hq-crew.timer section9-spend.timer section9-cpu.timer \
  section9-capacity.timer section9-sca.timer section9-cloudflared.timer
