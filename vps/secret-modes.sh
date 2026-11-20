#!/usr/bin/env bash
# Check the owner and mode of everything in the root-only secrets directory. Prints one
# line per problem and exits 1, or prints nothing and exits 0. Reads modes, never
# contents. install.sh stops on a problem; sync.sh reports it on the Sync issue.
#   directories (the top one, briefs/)  root, 0700
#   hermes_ssh_key                      uid 10000 (the Hermes image's user), 0400
#   sandbox_authorized_keys             root, 0644 (sshd in the sandbox reads it as the agent)
#   every other file                    root, 0600 or 0400
# Usage: secret-modes.sh [dir]   (default /etc/section9)
set -uo pipefail

dir=${1:-/etc/section9}
root=${S9_SECRETS_OWNER:-0}      # tests only; the VPS sets none

allowed() { # allowed <path>: the "uid:mode" values it may have, space-separated
  if [ -d "$1" ]; then echo "$root:700"; return; fi
  case ${1#"$dir"/} in
    hermes_ssh_key)          echo "10000:400" ;;
    sandbox_authorized_keys) echo "$root:644" ;;
    *)                       echo "$root:600 $root:400" ;;
  esac
}

[ -d "$dir" ] && [ ! -L "$dir" ] || { echo "$dir: missing or not a directory"; exit 1; }
bad=0
while IFS= read -r -d '' path; do
  if [ -L "$path" ] || { [ ! -f "$path" ] && [ ! -d "$path" ]; }; then
    echo "$path: not a plain file or directory"; bad=1; continue
  fi
  have=$(stat -c '%u:%a' "$path")
  want=$(allowed "$path")
  case " $want " in *" $have "*) continue ;; esac
  echo "$path: uid:mode is $have, want ${want// / or }"
  bad=1
done < <(find "$dir" -print0)
exit "$bad"
