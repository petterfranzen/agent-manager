#!/usr/bin/env bash
# Prepare the Hermes dashboard before compose starts it. Root, idempotent, prints no secret.
# Called by install.sh and sync.sh (a sync can apply a compose change without install.sh).
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
secrets=${S9_SECRETS_DIR:-/etc/section9}
state=${S9_STATE_DIR:-/var/lib/section9}

# Login: made once. Hermes refuses to serve the dashboard without it.
for k in USERNAME PASSWORD SECRET; do
  if ! grep -q "^HERMES_DASHBOARD_BASIC_AUTH_$k=" "$secrets/hermes.env"; then
    [[ -z $(tail -c1 "$secrets/hermes.env") ]] || echo >> "$secrets/hermes.env"
    echo "HERMES_DASHBOARD_BASIC_AUTH_$k=$(openssl rand -hex 16)" >> "$secrets/hermes.env"
    echo "Dashboard login made. Read it with: sudo grep DASHBOARD $secrets/hermes.env"
  fi
done

# Themes folder, with the starter theme until section9-hq-crew fetches hq/*.yaml.
install -d -m 0755 "$state/dashboard-themes"
[[ -e $state/dashboard-themes/ghost.yaml ]] || \
  install -m 0644 "$here/dashboard-themes/ghost.yaml" "$state/dashboard-themes/ghost.yaml"
