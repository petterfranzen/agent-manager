#!/usr/bin/env bash
# Creates /etc/agent-manager/ with every secret the stack needs. Run once, as
# root, on the VM. Generates internal secrets; prompts (without echo) for the
# OpenRouter key and the agents' GitHub token. Never overwrites existing files.
#
# Pasting into the Hetzner web console can drop or change characters in long
# strings, so each key may be pasted in several pieces (Enter after each, then
# Enter on an empty line), and each is checked live against its service before
# it's saved; a garbled paste is rejected and asked for again.
set -euo pipefail
umask 077
D=/etc/agent-manager
[[ $EUID -eq 0 ]] || { echo "Run as root (sudo)." >&2; exit 1; }
for tool in openssl ssh-keygen curl; do
  command -v "$tool" >/dev/null || { echo "Missing $tool (apt-get install -y openssl openssh-client curl)." >&2; exit 1; }
done
install -d -m 0700 "$D"

rand() { openssl rand -hex 32; }

# Reads a secret without echo, in one or more pieces; an empty line ends it.
read_secret() { # prompt -> stdout
  local piece all=""
  printf '%s\n  (paste it, Enter; more pieces + Enter if needed; then Enter on an empty line)\n' "$1" >&2
  while IFS= read -r -s piece; do
    piece=${piece//[[:space:]]/}
    [[ -z "$piece" ]] && break
    all+=$piece
    printf '  got %d characters so far\n' "${#all}" >&2
  done
  printf '%s' "$all"
}

# Asks until the service accepts the value (max 5 tries). $2 is a check command
# that gets the value as $1 and returns 0 if it works.
ask_checked() { # prompt check-fn -> stdout
  local v try
  for try in 1 2 3 4 5; do
    v=$(read_secret "$1")
    if [[ -n "$v" ]] && "$2" "$v"; then printf '%s' "$v"; return 0; fi
    echo "  Not accepted (try $try of 5). Paste it again, in smaller pieces if it keeps failing." >&2
  done
  echo "Giving up; nothing was saved for this key." >&2; return 1
}

check_openrouter() {
  [[ "$1" == sk-or-* ]] || { echo "  Doesn't start with sk-or-." >&2; return 1; }
  local code; code=$(curl -sS -o /dev/null -w '%{http_code}' -m 20 https://openrouter.ai/api/v1/key -H "Authorization: Bearer $1" || true)
  [[ "$code" == 200 ]] && { echo "  OpenRouter accepted the key." >&2; return 0; }
  echo "  OpenRouter said HTTP $code." >&2; return 1
}

check_github() {
  [[ "$1" == github_pat_* ]] || { echo "  Doesn't start with github_pat_." >&2; return 1; }
  # /user answers only for a valid token (the repo is public, so a repo request
  # would succeed even with a garbled one). Repo access shows at the first push.
  local body; body=$(curl -sS -m 20 https://api.github.com/user \
    -H "Authorization: Bearer $1" -H "Accept: application/vnd.github+json" || true)
  if grep -q '"login": *"petterfranzen"' <<<"$body"; then
    echo "  GitHub accepted the token (petterfranzen)." >&2; return 0
  fi
  echo "  GitHub didn't accept it (garbled, expired, or another account's token)." >&2; return 1
}
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
  OR_KEY=$(ask_checked "OpenRouter API key (input hidden):" check_openrouter)
  write_once hermes.env "OPENROUTER_API_KEY=$OR_KEY
API_SERVER_KEY=$(rand)"
  unset OR_KEY
else
  echo "keep   $D/hermes.env (exists)"
fi

if [[ ! -e "$D/sandbox.env" ]]; then
  GHT=$(ask_checked "GitHub token agents-ghost (input hidden):" check_github)
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
