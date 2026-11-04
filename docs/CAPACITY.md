# Capacity log

Petter's rule (4 Nov): spend the Major's Claude Pro week wisely but fully at every
weekly reset; use the full Hostinger capacity without tripping the throttle; use the
$40 OpenRouter budget fully each month. Metrics come first, forecasts and any
request for more budget come after, backed by this data. The VPS side (per-run
ledger, daily "Capacity" issue) is section9-hq #67.

## Claude Pro (the Major)

The Major cannot read its own usage. At each weekly reset Petter pastes the usage
report and the Major adds a row here. Mid-week readings are welcome too.

| Reading | Weekly used | Time elapsed | Resets (UTC) | Note |
|---|---|---|---|---|
| 2026-11-04 17:47 UTC | 50% | 56% (95 of 168 h) | 2026-11-07 19:00 | About 6 points under pace: room for ~0.7% an hour (~16% a day) until the reset. Session window 5% used. |
| 2026-11-04 19:30 UTC | 53% | 57% (96.5 of 168 h) | 2026-11-07 19:00 | First automatic reading (tools/pro-usage, section9-hq #81). +3 points in 1.7 h while the Major reviewed, merged and queued work: about 1.7% an hour against 0.66% an hour left in the budget (47% over 71.5 h). At this rate the week runs out about 28 h before the reset. Session window 10%. |

What goes to Pro: review, merge, planning, the judgement calls. What goes to the
agents: research, builds, QA, anything that can wait overnight. Under pace by a
margin like the above, the Major can take on more review and planning work; over
pace, it hands more to second reviewers and agents.

Rule of thumb: a week is 14.3% a day, so a Thursday reading should be around 57%.
Over 60% by Thursday: move small-PR QA and mechanical reviews to a non-author agent.
Under 40% by Thursday: batch the week's reviews, plans and briefs into Pro rather
than spreading them thin. Keep roughly 25-35% of the week for the end-of-week merges
and surprises. Log the percentage, the reset time and the week's Pro work (reviews,
merges, plans); that is what calibrates "one review costs about this much of a week".

## OpenRouter ($40 a month, cap raised 4 Nov)

Two samples only, before the ledger: about $0.09 an hour on a day of QA runs, about
$0.16 an hour averaged over 1-4 Nov (heavy builds and retries). The per-run ledger
replaces these with measured numbers.

At 17:10 UTC on 4 Nov: $14.42 of $40 used, day 3.7 of 30. The fixed linear pace
(($40 - $1) / 30 = $1.30 a day) puts the line at $4.83, so the month is 3.0x ahead of
it. That is not a fault: the cap was $20 until that morning, and $14.42 against the
old $19 line was 6.1x. The line that matters after a cap change is the remaining one:
$24.58 left after the $1 reserve over 26.3 days = **$0.94 a day**. The Capacity issue
shows both lines every day.

## Hostinger VPS

Measured at 17:04 UTC on 4 Nov: limit off, one thread gets 0.97 core, steal 1.8%,
busy 9.3% of a vCPU, PSI avg60 23.8. The old draw figure read 117,609 credits/hour;
that was the `/proc/stat` tick bug (section9-hq #44). 118,509 ticks an hour is 1,185
core-seconds, minus the 900-second baseline = **~285 credits/hour**, 3.5% of the
8,100 balance, an average of about a third of a core over that hour.

The ceiling: one 8,100-credit balance a day is `8,100 / 86,400 = 0.094` core above
baseline, i.e. **24-34% average of one vCPU** depending on whether the real baseline
is 15%, 20% or the contractual 25%. At the existing 0.70-core caps (sandbox 0.40 +
Hermes 0.30), one busy hour costs `(0.70 - baseline) x 3600` = 1,620-1,980 credits,
so the day allows **4.1-5.0 busy hours**. Run **4 h a day sustained, never more than
5 h in any 24 h**; a 3 h Tachikoma night leaves about one daytime run. Petter's graph
(15 h near 100%, then a 40% limit) is 40,500 credits above baseline, three to four
times the daily ceiling; that is why the cap came back, not a reason to spend more.

What the VPS cannot see: Hostinger's actual balance and refill rate (no API key).
If the panel falls over a week at 4 h/day, lower the ceiling; if it stays full, there
is more headroom. Check the hourly **CPU credits** issue and the panel weekly.

## The run ledger

`vps/dispatch.py` appends one JSON line per admitted run to
`/var/lib/section9/runs.jsonl` (outside the repo; `--ledger` overrides it). A failed
write never stops or fails a run: the dispatcher logs `ledger: not written` and goes
on. Single lane makes the deltas attributable to one run; with two lanes, read the
record as the pair's cost.

| field | meaning |
|---|---|
| `issue`, `agent`, `role`, `type`, `model` | what ran; `type` comes from the title prefix (`QA:`, `Research:`, `Design:` …) |
| `start`, `end`, `seconds` | UTC ISO times and wall-clock length |
| `outcome` | `ok`, `timeout`, `blocked`, `no-output` |
| `queue_before`, `held_low_priority` | todo issues seen at start; how many the pace rule held |
| `usage_before`, `usage_after` | OpenRouter `/key`: `usage_monthly`, `usage_daily`, `limit`, `limit_remaining` |
| `cost_usd` | the `usage_monthly` delta; `null` when the counter did not move or could not be read |
| `tokens` | Hermes's `usage` object, when the reply carries one |
| `cpu`, `cpu_before`, `cpu_after` | `wall_seconds`, `steal_pct`, `core_seconds`, `credits`, plus the raw snapshots |

## The pacing rule

The dispatcher keeps the existing fail-closed spend check and the $1 reserve. Pacing
is an extra, **off by default**, in the root-only `/etc/section9/limits.toml`:

```toml
[pacing]
enabled = true        # false or absent: no behaviour change
slack_days = 1.0      # how far ahead of the line is still "on pace"
steal_pct = 10.0      # above this, Hostinger's limit is on
sample_seconds = 1.0  # CPU sample before an admitted run
```

1. `linear pace = ($cap - $1) / days_in_month x days_elapsed`; the report also shows
   `remaining pace = ($cap - spend - $1) / days_left`, which re-baselines after a cap
   change (as it did from $20 to $40 on 4 Nov).
2. Under the line and steal at or below 10%: admit normal work and `priority:low` work.
3. Over the line by more than `slack_days`, or steal above the threshold: hold only
   `priority:low` issues; ordinary work still runs. If the CPU figures cannot be read,
   hold low-priority work (fail closed, but the spend check still runs first).
4. Nothing is admitted when the spend check or the cap fails, exactly as today.

The one-lane default does not change. When the `lanes` switch in the same root-only
file allows two lanes (#61; not implemented as of 4 Nov), both lanes must evaluate the
same pace and the same steal sample, and the lock has to become per-lane. The CPU
ceiling is for the whole VPS, so run two lanes only while the 7-day credits/day stay
under 8,100; if a day goes over, turn `lanes` back to 1. A low-priority hold applies
to both lanes.

## The Capacity issue

`section9-capacity.timer` runs `dispatch.py capacity` once a day and rewrites one
open **Capacity** issue in `section9-hq` (pinned like **Spend**): last 7 days and
month to date, runs, busy hours, $ and $/hour by agent and task type, timeouts and the
hours they wasted, queue idle (an upper bound: the ledger only sees runs), CPU credits
against the daily guidance, and both pace lines with today's spend against them. It
guesses nothing: a run whose cost delta was unreadable is reported as `—`, and the
issue says how many such runs there are.

## Forecast and the budget request (the Major's procedure)

Do this after **14 days or 50 runs**, whichever is later; before that the sample is
too small to forecast.

1. Read the Capacity issue's 7-day table. For each task type: median cost per run,
   median busy hours per run, and $/busy hour.
2. `affordable hours/day = (cap - spend - $1) / days_left / blended $/hour`.
3. `affordable runs/day` per type = `affordable hours/day / median hours` for that type.
4. Forecast next month: for each planned task type, `runs x median $/run`, summed;
   compare with the cap. State the sample size and the range, not one number.
5. If the forecast is above the cap, ask for the difference with this note:

```
Budget request, <month>
Measured on <N> runs over <D> days (ledger, VPS).
Blended cost: $<x.xx>/busy hour; median run <h> h.
At $<cap>/month we can afford <H> busy hours/day, i.e. <R> runs/day of the
current mix. The plan for <month> needs <H'> hours/day (<R'> runs): $<F>.
Request: raise the cap to $<F + 1 reserve>, or drop <K> low-priority runs.
CPU: <C> credits/day, <P>% of the 8,100/day guidance.
```

## Still to collect

- Two weeks of ledger lines: every per-type cost, timeout rate and queue-idle figure
  is empty or an upper bound until then.
- Hostinger's credit balance and refill behaviour from the panel (Petter, weekly).
- Claude Pro's usage percentage at each reset (Petter).
- Hermes token counts, if the gateway starts returning `usage`; today the ledger
  records them only when present.
