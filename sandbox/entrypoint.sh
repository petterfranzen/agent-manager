#!/usr/bin/env bash
# SSH sessions don't inherit the container's environment, so hand the GitHub
# token to the agent user as files git and gh read (mode 0600), then start sshd.
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

# The host key lives on a volume so it survives restarts and rebuilds;
# otherwise Hermes would see a changed host key and refuse to connect.
mkdir -p /etc/ssh/hostkeys
[[ -f /etc/ssh/hostkeys/ssh_host_ed25519_key ]] || ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/hostkeys/ssh_host_ed25519_key
exec /usr/sbin/sshd -D -e
