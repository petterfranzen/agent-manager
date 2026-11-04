# Pro usage: let the Major see the Claude Pro week

Petter, 4 Nov: the Major can't read its own plan usage, and `/usage` is
interactive. Claude Code's status line payload carries it
(`rate_limits.five_hour` and `rate_limits.seven_day`, each with
`used_percentage` and `resets_at`; Pro/Max/Ultra only, after the first reply of
a session; docs: https://code.claude.com/docs/en/statusline.md). Two small
scripts turn that into data points the Major reads from the private board.

- `statusline.sh`: the status line command. Saves the latest reading to
  `~/.claude/pro-usage-latest.json` and prints `Pro 5h 23% · wk 50%`.
- `post.sh`: publishes the reading to the pinned issue "Pro usage"
  (label `pro-usage`) in `petterfranzen/section9-hq`, using your own `gh` login.
  The body is the latest reading; a comment is added at most once an hour as a
  data point. Nothing is posted when the reading is older than two hours.
- `test.sh`: offline check of `statusline.sh`.

## Install (on your machine, e.g. darkstar)

Needs `jq` and `gh` (logged in as you: `gh auth status`).

1. Put the repo somewhere stable, then in `~/.claude/settings.json`:
   `"statusLine": {"type": "command", "command": "/path/to/agent-manager/tools/pro-usage/statusline.sh"}`
   If you already have a status line, call this script from it (pipe the same
   stdin) and keep its output.
2. Run `post.sh` every 15 to 30 minutes, e.g. cron:
   `*/20 * * * * /path/to/agent-manager/tools/pro-usage/post.sh >/dev/null 2>&1`
3. Open Claude Code once and send a message, so the first reading is written.

The reading is only as fresh as the last Claude Code session you had open:
when the machine is off or Claude Code is closed there is no new data point,
which is fine. The Major then uses the last one and says how old it is.

Only percentages and reset times are posted; no tokens, account details or
prompt content leave the machine.
