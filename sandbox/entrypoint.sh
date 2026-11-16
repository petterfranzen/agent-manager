#!/usr/bin/env bash
# SSH sessions don't inherit the container's environment, so hand the GitHub
# token to the agent user as files git and gh read (mode 0600), and the optional
# CodeScene token as the file the `cs` wrapper reads, then start sshd.
set -euo pipefail

home=/home/agent
if [[ -n "${GH_TOKEN:-}" ]]; then
  umask 077
  printf 'https://x-access-token:%s@github.com\n' "$GH_TOKEN" > "$home/.git-credentials"
  # gh's multi-account layout (with "users:"), so gh doesn't try to migrate it.
  printf 'github.com:\n    users:\n        x-access-token:\n            oauth_token: %s\n    git_protocol: https\n    oauth_token: %s\n    user: x-access-token\n' \
    "$GH_TOKEN" "$GH_TOKEN" > "$home/.config/gh/hosts.yml"
  # version "1" marks the config as already migrated to the multi-account format.
  printf 'version: "1"\ngit_protocol: https\n' > "$home/.config/gh/config.yml"
  chown agent:agent "$home/.git-credentials" "$home/.config/gh/hosts.yml" "$home/.config/gh/config.yml"
fi
unset GH_TOKEN

# Optional: without /etc/section9/codescene.env there is no token and no file.
rm -f "$home/.config/codescene/token"
if [[ -n "${CS_ACCESS_TOKEN:-}" ]]; then
  install -d -m 0700 -o agent -g agent "$home/.config/codescene"
  (umask 077; printf '%s\n' "$CS_ACCESS_TOKEN" > "$home/.config/codescene/token")
  chown agent:agent "$home/.config/codescene/token"
fi
unset CS_ACCESS_TOKEN

# The host key lives on a volume so it survives restarts and rebuilds;
# otherwise Hermes would see a changed host key and refuse to connect.
mkdir -p /etc/ssh/hostkeys
[[ -f /etc/ssh/hostkeys/ssh_host_ed25519_key ]] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/hostkeys/ssh_host_ed25519_key
exec /usr/sbin/sshd -D -e
