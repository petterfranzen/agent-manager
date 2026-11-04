#!/usr/bin/env bash
# Publish the latest Claude Pro usage reading to the pinned "Pro usage" issue
# in the private repo petterfranzen/section9-hq, using YOUR gh login.
#   - the issue body always shows the latest reading;
#   - a comment (one data point for the Major's forecasts) is added at most
#     once per PRO_USAGE_MIN_MINUTES (default 55).
# Run it from cron or a systemd timer every 15-30 minutes. If the machine was
# off or no Claude Code session was open, the reading is older than
# PRO_USAGE_MAX_AGE_MINUTES (default 120) and nothing is posted.
set -eu
repo="${PRO_USAGE_REPO:-petterfranzen/section9-hq}"
src="${PRO_USAGE_FILE:-$HOME/.claude/pro-usage-latest.json}"
state="${PRO_USAGE_STATE:-$HOME/.claude/pro-usage-last-post}"
min_minutes="${PRO_USAGE_MIN_MINUTES:-55}"
max_age="${PRO_USAGE_MAX_AGE_MINUTES:-120}"

[ -s "$src" ] || { echo "no reading yet"; exit 0; }
now="$(date -u +%s)"
at="$(jq -r '.at' "$src")"
at_s="$(date -u -d "$at" +%s)"
if [ $(( (now - at_s) / 60 )) -gt "$max_age" ]; then echo "reading is stale ($at)"; exit 0; fi

iso() { [ -n "$1" ] && [ "$1" != null ] && date -u -d "@$1" +%Y-%m-%dT%H:%MZ || echo "unknown"; }
h="$(jq -r '.five_hour_pct // "?"' "$src")"; hr="$(iso "$(jq -r '.five_hour_reset' "$src")")"
w="$(jq -r '.weekly_pct // "?"' "$src")";    wr="$(iso "$(jq -r '.weekly_reset' "$src")")"
line="- Weekly: **${w}%** used, resets ${wr}
- 5-hour session: **${h}%** used, resets ${hr}
- Reading taken: ${at}"

number="$(gh api "repos/$repo/issues?state=open&labels=pro-usage&per_page=1" --jq '.[0].number // empty')"
if [ -z "$number" ]; then
  number="$(gh api "repos/$repo/issues" -f title="Pro usage" -f 'labels[]=pro-usage' \
    -f body="Created by tools/pro-usage/post.sh." --jq .number)"
fi
body="**Live snapshot of Petter's Claude Pro plan usage.** Posted by tools/pro-usage/post.sh from his machine; as of ${at}.

${line}

Rewritten on each post; the comments are the history. Leave this issue open."
gh api -X PATCH "repos/$repo/issues/$number" -f body="$body" >/dev/null

last=0; [ -s "$state" ] && last="$(cat "$state")"
if [ $(( (now - last) / 60 )) -ge "$min_minutes" ]; then
  gh api "repos/$repo/issues/$number/comments" -f body="$at weekly ${w}% (resets ${wr}), 5h ${h}% (resets ${hr})" >/dev/null
  printf '%s\n' "$now" > "$state"
  echo "posted data point to #$number"
else
  echo "updated #$number (data point throttled)"
fi
