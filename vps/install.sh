#!/usr/bin/env bash
# Set up Section 9 on the VPS. Run as root; safe to re-run.
# Usage: sudo /opt/agent-manager/vps/install.sh
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
secrets=/etc/section9
swapfile=/swapfile

[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
command -v docker >/dev/null || { echo "Docker is not installed." >&2; exit 1; }

# 1. Secrets first, so nothing is changed if they are missing.
missing=0
for f in hermes.env sandbox.env; do
  if [[ ! -f $secrets/$f ]]; then echo "MISSING: $secrets/$f" >&2; missing=1
  elif [[ $(stat -c '%a %U' "$secrets/$f") != "600 root" ]]; then
    echo "WRONG MODE/OWNER: $secrets/$f (want 0600 root; fix: chown root:root $secrets/$f && chmod 600 $secrets/$f)" >&2; missing=1
  fi
done
for f in hermes_ssh_key sandbox_authorized_keys; do
  [[ -f $secrets/$f ]] || { echo "MISSING: $secrets/$f" >&2; missing=1; }
done
[[ $missing -eq 0 ]] || { echo "Copy the secrets first (docs/RUNBOOK.md, VPS step 1)." >&2; exit 1; }

# sshd in the sandbox refuses a key file it can't read as the agent user: root-owned, 0644.
chown root:root "$secrets/sandbox_authorized_keys"
chmod 0644 "$secrets/sandbox_authorized_keys"

# Dashboard login and themes folder (also done by sync.sh before it applies a compose change).
"$here/dashboard-prep.sh"

# 2. 4 GB swap.
if ! swapon --show=NAME --noheadings | grep -qx "$swapfile"; then
  if [[ ! -f $swapfile ]]; then
    fallocate -l 4G "$swapfile"
    chmod 600 "$swapfile"
    mkswap "$swapfile" >/dev/null
  fi
  swapon "$swapfile"
fi
grep -qE "^$swapfile\s" /etc/fstab || echo "$swapfile none swap sw 0 0" >> /etc/fstab

# 3. Our Hermes + sandbox + the HQ page server. The page's files are written here by timers.
install -d -m 0755 /var/lib/section9/hq /var/lib/section9/dashboard-themes
docker compose -f "$here/compose.yaml" up -d --build
# A leftover Impeccable skill on the Hermes volume (#50 removed it from the sandbox) is pushed
# into the sandbox by Hermes's file sync, ~24 MB each time. Idempotent.
docker compose -f "$here/compose.yaml" exec -T hermes rm -rf /opt/data/skills/impeccable

# 4. Retire Hostinger's Hermes and Traefik (cloudflared stays).
old=$(docker ps -a --format '{{.ID}} {{.Image}}' | grep -E 'hostinger/hvps-hermes-agent|traefik' | cut -d' ' -f1 || true)
if [[ -n $old ]]; then
  # shellcheck disable=SC2086
  docker update --restart=no $old >/dev/null
  # shellcheck disable=SC2086
  docker rm -f $old >/dev/null
  echo "Removed Hostinger's containers: $(echo "$old" | wc -l)"
fi

# 5. Timers: dispatcher, routines, sync (pulls main once CI is green), HQ page, spend, CPU.
"$here/install-units.sh"

echo "Done. Check: python3 -I $here/dispatch.py status && python3 -I $here/dispatch.py cpu"
