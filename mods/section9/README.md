# section9: a Claude Code pane for the Section 9 crew

A Claude Code mod that opens a **Section 9** pane beside the chat. It shows what each agent (Batou, Ishikawa, Saito, Togusa, Tachikoma) is doing and what the crew costs:

- **Budget:** this month's OpenRouter spend, the cap, what's left and today's spend, from the hourly "Spend" issue on the task board, with its as-of time in CET. It turns red when that figure is more than 3 hours old.
- **Each agent:** model, runs-per-day cap and monthly cap (from `agents/roster.toml` on main), the last 7 days' runs, hours, spend and timeouts (from the "Capacity" issue), and its open tasks, coloured by status (working, todo, review, blocked) with who each one waits on.

It refreshes every 5 minutes, and has a Refresh button. `/section9` reopens the pane.

## Install

In a terminal Claude Code session on your own machine:

```
/plugin install section9 --marketplace petterfranzen/agent-manager
```

Answer `y` to add the marketplace, then pick a scope (user scope makes it available in every session).

It needs the GitHub CLI (`gh`) logged in with read access to the private task board. It reads everything through `gh api` and stores no tokens. The pane draws in terminal Claude Code. Some desktop builds don't draw mod panes yet; the same view is on the hq board page.
