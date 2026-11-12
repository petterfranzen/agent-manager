#!/usr/bin/env bash
# Hold cloudflared to Petter's patch rule (docs/DECISIONS.md, Principles): a
# release is installed once it is 24 h old, so a bad release is caught before it
# reaches the VPS. cloudflared comes from Cloudflare's own apt repo, so
# unattended-upgrades is told to leave it alone (vps/apt/52section9-cloudflared)
# and this script is the only thing that upgrades it. It records the first time it
# sees a candidate version and installs it only once that release has been
# available for S9_CLOUDFLARED_DELAY_H hours (24 by default). Run by
# section9-cloudflared.timer; safe to re-run.
#
#   cloudflared-patch.sh            enforce the 24 h delay
#   cloudflared-patch.sh --now      install the candidate now: the CVE fast path
#                                   (CVSS 7+ or CISA KEV), inside the 72 h bound
#   cloudflared-patch.sh --dry-run  decide and print the action, change nothing
#   cloudflared-patch.sh --status   print installed, candidate and age
#
# Exit: 0 = up to date, holding, or applied; 1 = apt could not be read or the
# upgrade failed.
set -euo pipefail

PKG=cloudflared
DELAY=$(( ${S9_CLOUDFLARED_DELAY_H:-24} * 3600 ))
STATE_DIR=${S9_STATE_DIR:-/var/lib/section9}
STAMP=$STATE_DIR/cloudflared-candidate

dry=0
force=0
status=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    --now)     force=1 ;;
    --status)  status=1 ;;
    *) echo "usage: cloudflared-patch.sh [--now] [--dry-run] [--status]" >&2; exit 2 ;;
  esac
done

now=$(date +%s)
installed=$(dpkg-query -W -f='${Version}' "$PKG" 2>/dev/null || true)
candidate=$(apt-cache policy "$PKG" 2>/dev/null | awk '/Candidate:/{print $2; exit}')

if [ -z "${candidate:-}" ] || [ "$candidate" = "(none)" ]; then
  echo "cloudflared-patch: no candidate version from apt for $PKG (repo not configured?)" >&2
  exit 1
fi

# Seconds since the candidate was first seen, or nothing when it has not been.
age_of_candidate() {
  [ -f "$STAMP" ] || return 0
  local ver at
  read -r ver at <"$STAMP" || true
  [ "${ver:-}" = "$candidate" ] && [ -n "${at:-}" ] && echo "$(( now - at ))"
}

if [ "$candidate" = "${installed:-}" ]; then
  [ "$status" = 1 ] || rm -f "$STAMP"
  echo "cloudflared-patch: up to date ($installed)"
  exit 0
fi

if [ "$status" = 1 ]; then
  age=$(age_of_candidate)
  if [ -n "${age:-}" ]; then
    echo "cloudflared-patch: installed ${installed:-none}, candidate $candidate, seen $(( age / 3600 )) h ago"
  else
    echo "cloudflared-patch: installed ${installed:-none}, candidate $candidate, not yet seen"
  fi
  exit 0
fi

if [ "$force" = 0 ]; then
  age=$(age_of_candidate)
  if [ -z "${age:-}" ]; then
    install -d -m 0755 "$STATE_DIR"
    printf '%s %s\n' "$candidate" "$now" >"$STAMP"
    echo "cloudflared-patch: $candidate is new; holding it for $(( DELAY / 3600 )) h"
    exit 0
  fi
  if [ "$age" -lt "$DELAY" ]; then
    echo "cloudflared-patch: holding $candidate; $(( (DELAY - age + 3599) / 3600 )) h to go"
    exit 0
  fi
fi

if [ "$dry" = 1 ]; then
  echo "cloudflared-patch: would upgrade ${installed:-none} -> $candidate"
  exit 0
fi

echo "cloudflared-patch: upgrading ${installed:-none} -> $candidate"
DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade "$PKG"
rm -f "$STAMP"
echo "cloudflared-patch: upgraded to $candidate"
