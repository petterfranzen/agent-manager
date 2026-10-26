#!/usr/bin/env bash
# Creates /etc/agent-manager/ with every secret the stack needs. Run once, as
# root, on the VM. Generates internal secrets; prompts (without echo) for the
# OpenRouter key and the agents' GitHub token. Never overwrites existing files.
set -euo pipefail
umask 077
D=/etc/agent-manager
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
for tool in openssl ssh-keygen; do
  command -v "$tool" >/dev/null || { echo "Missing $tool (apt-get install -y openssl openssh-client)." >&2; exit 1; }
done
install -d -m 0700 "$D"

rand() { openssl rand -hex 32; }
write_once() { # file content
  if [[ -e "$D/$1" ]]; then echo "keep   $D/$1 (exists)"; else printf '%s\n' "$2" > "$D/$1"; chmod 0600 "$D/$1"; echo "wrote  $D/$1"; fi
}

if [[ ! -e "$D/postgres.env" ]]; then
  PGPW=$(rand)
  write_once postgres.env "POSTGRES_PASSWORD=$PGPW"
  write_once paperclip.env "BETTER_AUTH_SECRET=$(rand)
DATABASE_URL=postgres://paperclip:$PGPW@postgres:5432/paperclip"
else
  echo "keep   $D/postgres.env and paperclip.env (exist)"
fi

if [[ ! -e "$D/hermes.env" ]]; then
  read -r -s -p "OpenRouter API key (input hidden): " OR_KEY; echo
  [[ "$OR_KEY" == sk-or-* ]] || { echo "That doesn't look like an OpenRouter key (sk-or-...)." >&2; exit 1; }
  write_once hermes.env "OPENROUTER_API_KEY=$OR_KEY
API_SERVER_KEY=$(rand)"
  unset OR_KEY
else
  echo "keep   $D/hermes.env (exists)"
fi

if [[ ! -e "$D/sandbox.env" ]]; then
  read -r -s -p "GitHub token for ghost-in-the-machine (input hidden): " GHT; echo
  [[ "$GHT" == github_pat_* ]] || { echo "That doesn't look like a fine-grained token (github_pat_...)." >&2; exit 1; }
  write_once sandbox.env "GH_TOKEN=$GHT"
  unset GHT
else
  echo "keep   $D/sandbox.env (exists)"
fi

if [[ ! -e "$D/hermes_ssh_key" ]]; then
  ssh-keygen -q -t ed25519 -N '' -C hermes-to-sandbox -f "$D/hermes_ssh_key"
  mv "$D/hermes_ssh_key.pub" "$D/sandbox_authorized_keys"
  chown 10000:10000 "$D/hermes_ssh_key"; chmod 0400 "$D/hermes_ssh_key"
  chmod 0644 "$D/sandbox_authorized_keys"
  echo "wrote  $D/hermes_ssh_key and sandbox_authorized_keys"
else
  echo "keep   $D/hermes_ssh_key (exists)"
fi

echo "Done. Files in $D:"; ls -l "$D"
