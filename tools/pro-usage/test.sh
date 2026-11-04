#!/usr/bin/env bash
# Offline check of statusline.sh: feeds sample payloads, no network.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
export PRO_USAGE_FILE="$tmp/latest.json"

with='{"model":{"id":"x"},"rate_limits":{"five_hour":{"used_percentage":23.5,"resets_at":1793000000},"seven_day":{"used_percentage":50,"resets_at":1793200000}}}'
line="$(printf '%s' "$with" | "$here/statusline.sh")"
[ "$line" = "Pro 5h 23.5% · wk 50%" ] || { echo "bad line: $line"; exit 1; }
[ "$(jq -r .weekly_pct "$PRO_USAGE_FILE")" = "50" ] || { echo "weekly missing"; exit 1; }
[ "$(jq -r .five_hour_reset "$PRO_USAGE_FILE")" = "1793000000" ] || { echo "reset missing"; exit 1; }

rm -f "$PRO_USAGE_FILE"
line="$(printf '%s' '{"model":{"id":"x"}}' | "$here/statusline.sh")"
[ "$line" = "Claude Code" ] || { echo "bad fallback: $line"; exit 1; }
[ ! -e "$PRO_USAGE_FILE" ] || { echo "wrote a file with no usage"; exit 1; }

# only one window present (each can be absent on its own)
line="$(printf '%s' '{"rate_limits":{"seven_day":{"used_percentage":12,"resets_at":1793200000}}}' | "$here/statusline.sh")"
[ "$line" = "Pro 5h ?% · wk 12%" ] || { echo "bad partial: $line"; exit 1; }
echo ok
