#!/usr/bin/env bash
# Claude Code status line command that also records plan usage.
# Claude Code pipes a JSON payload to this script on stdin; since it carries
# rate_limits.five_hour and rate_limits.seven_day (Pro/Max/Ultra only, after
# the first reply of a session), we keep the latest reading in a file for
# post.sh to publish. Prints a short status line.
#
# Install (see README.md): "statusLine": {"type": "command", "command": "<path>/statusline.sh"}
set -u
out="${PRO_USAGE_FILE:-$HOME/.claude/pro-usage-latest.json}"
payload="$(cat)"

reading="$(printf '%s' "$payload" | jq -c --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" '
  select(.rate_limits != null)
  | { at: $now,
      five_hour_pct:   (.rate_limits.five_hour.used_percentage // null),
      five_hour_reset: (.rate_limits.five_hour.resets_at // null),
      weekly_pct:      (.rate_limits.seven_day.used_percentage // null),
      weekly_reset:    (.rate_limits.seven_day.resets_at // null) }' 2>/dev/null)"

if [ -n "$reading" ]; then
  mkdir -p "$(dirname "$out")"
  tmp="$out.$$"
  printf '%s\n' "$reading" > "$tmp" && mv "$tmp" "$out"
  printf '%s' "$reading" | jq -r '"Pro 5h \(.five_hour_pct // "?")% · wk \(.weekly_pct // "?")%"'
else
  printf 'Claude Code'
fi
