#!/usr/bin/env bash
# Hold the container runtime to Petter's patch rule (docs/DECISIONS.md,
# Principles): a release is installed once it is 24 h old, so a bad release is
# caught before it reaches the VPS. On the VPS docker-ce, containerd.io and runc
# come from Docker's own apt repo (download.docker.com); a box built from
# docs/RUNBOOK.md step 1 has Ubuntu's docker.io/containerd/runc instead. Either
# way unattended-upgrades is told to leave them alone
# (vps/apt/52section9-container-runtime) and this script is the only upgrader: it
# records the first time it sees a candidate for each package and installs it
# only once that release has been available for S9_CONTAINER_DELAY_H hours (24 by
# default). After an upgrade it restarts containerd and dockerd, because a
# running daemon keeps the pre-upgrade binary in memory and the containers would
# otherwise keep running the old runtime. Run by
# section9-container-runtime-patch.timer; safe to re-run.
#
#   container-runtime-patch.sh            enforce the 24 h delay
#   container-runtime-patch.sh --now      install every candidate now: the CVE
#                                         fast path (CVSS 7+ or CISA KEV), inside
#                                         the 72 h bound
#   container-runtime-patch.sh --dry-run  decide and print the action, change nothing
#   container-runtime-patch.sh --status   print installed, candidate and age per package
#
# S9_CONTAINER_PKGS overrides the package list; a name not installed is skipped,
# so one list serves both the docker-ce and the docker.io host.
# S9_CONTAINER_PATCH_NO_RESTART=1 upgrades without the containerd/dockerd restart,
# for a CVE applied to a busy box where the restart moves to a quiet hour.
#
# Exit: 0 = up to date, holding, or applied; 1 = apt could not be read, a package
# had no candidate, or the upgrade failed.
set -euo pipefail

DELAY=$(( ${S9_CONTAINER_DELAY_H:-24} * 3600 ))
STATE_DIR=${S9_STATE_DIR:-/var/lib/section9}
STAMP_DIR=$STATE_DIR/container-runtime-candidates
read -r -a PKGS <<<"${S9_CONTAINER_PKGS:-docker-ce docker-ce-cli docker-buildx-plugin docker-compose-plugin containerd.io docker.io containerd runc}"

dry=0
force=0
status=0
restart=1
for arg in "$@"; do
  case "$arg" in
    --dry-run) dry=1 ;;
    --now)     force=1 ;;
    --status)  status=1 ;;
    *) echo "usage: container-runtime-patch.sh [--now] [--dry-run] [--status]" >&2; exit 2 ;;
  esac
done
[ "${S9_CONTAINER_PATCH_NO_RESTART:-0}" = 1 ] && restart=0

now=$(date +%s)
found=0
upgraded=0

# Seconds since this package's candidate was first seen, or nothing when the
# stamp is missing or names another version.
age_of_candidate() {
  local stamp=$STAMP_DIR/$1 ver at
  [ -f "$stamp" ] || return 0
  read -r ver at <"$stamp" || true
  [ "${ver:-}" = "$2" ] && [ -n "${at:-}" ] && echo "$(( now - at ))"
}

handle_pkg() {
  local pkg=$1 installed candidate age stamp
  installed=$(dpkg-query -W -f='${Version}' "$pkg" 2>/dev/null || true)
  [ -n "$installed" ] || return 0            # not installed on this host
  found=$(( found + 1 ))
  candidate=$(apt-cache policy "$pkg" 2>/dev/null | awk '/Candidate:/{print $2; exit}')
  if [ -z "${candidate:-}" ] || [ "$candidate" = "(none)" ]; then
    echo "container-runtime-patch: no candidate version from apt for $pkg (repo not configured?)" >&2
    return 1
  fi
  stamp=$STAMP_DIR/$pkg
  if [ "$candidate" = "$installed" ]; then
    [ "$status" = 1 ] || rm -f "$stamp"
    echo "container-runtime-patch: $pkg up to date ($installed)"
    return 0
  fi
  age=$(age_of_candidate "$pkg" "$candidate")
  if [ "$status" = 1 ]; then
    if [ -n "${age:-}" ]; then
      echo "container-runtime-patch: $pkg installed $installed, candidate $candidate, seen $(( age / 3600 )) h ago"
    else
      echo "container-runtime-patch: $pkg installed $installed, candidate $candidate, not yet seen"
    fi
    return 0
  fi
  if [ "$force" = 0 ]; then
    if [ -z "${age:-}" ]; then
      install -d -m 0755 "$STAMP_DIR"
      printf '%s %s\n' "$candidate" "$now" >"$stamp"
      echo "container-runtime-patch: $pkg $candidate is new; holding it for $(( DELAY / 3600 )) h"
      return 0
    fi
    if [ "$age" -lt "$DELAY" ]; then
      echo "container-runtime-patch: $pkg holding $candidate; $(( (DELAY - age + 3599) / 3600 )) h to go"
      return 0
    fi
  fi
  if [ "$dry" = 1 ]; then
    echo "container-runtime-patch: would upgrade $pkg $installed -> $candidate"
    return 0
  fi
  echo "container-runtime-patch: upgrading $pkg $installed -> $candidate"
  DEBIAN_FRONTEND=noninteractive apt-get install -y --only-upgrade "$pkg"
  rm -f "$stamp"
  echo "container-runtime-patch: upgraded $pkg to $candidate"
  upgraded=$(( upgraded + 1 ))
  return 0
}

rc=0
for pkg in "${PKGS[@]}"; do
  handle_pkg "$pkg" || rc=1
done

if [ "$found" = 0 ]; then
  echo "container-runtime-patch: none of the runtime packages are installed (${PKGS[*]})" >&2
  exit 1
fi

if [ "$upgraded" -gt 0 ]; then
  if [ "$restart" = 1 ]; then
    echo "container-runtime-patch: restarting containerd and docker so the running runtime is the new one"
    systemctl try-restart containerd.service || true
    systemctl try-restart docker.service || true
  else
    echo "container-runtime-patch: upgraded $upgraded package(s); restart containerd and docker to pick it up"
  fi
fi
exit "$rc"
