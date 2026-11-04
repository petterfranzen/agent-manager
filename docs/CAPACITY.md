# Capacity log

Petter's rule (4 Nov): spend the Major's Claude Pro week wisely but fully at every
weekly reset; use the full Hostinger capacity without tripping the throttle; use the
$40 OpenRouter budget fully each month. Metrics come first, forecasts and any
request for more budget come after, backed by this data. The VPS side (per-run
ledger, weekly "Capacity" issue) is section9-hq #67.

## Claude Pro (the Major)

The Major cannot read its own usage. At each weekly reset Petter pastes the usage
report and the Major adds a row here. Mid-week readings are welcome too.

| Reading | Weekly used | Time elapsed | Resets (UTC) | Note |
|---|---|---|---|---|
| 2026-11-04 17:47 UTC | 50% | 56% (95 of 168 h) | 2026-11-07 19:00 | About 6 points under pace: room for ~0.7% an hour (~16% a day) until the reset. Session window 5% used. |

What goes to Pro: review, merge, planning, the judgement calls. What goes to the
agents: research, builds, QA, anything that can wait overnight. Under pace by a
margin like the above, the Major can take on more review and planning work; over
pace, it hands more to second reviewers and agents.

## OpenRouter ($40 a month, cap raised 4 Nov)

Linear pace is $1.33 a day. Observed so far: about $0.09 an hour on a day of QA
runs, about $0.16 an hour averaged over 1-4 Nov (heavy builds and retries). Two
samples only; the per-run ledger replaces this with real numbers.

## Hostinger VPS

Safe sustained CPU use and busy hours a day are not yet established. Inputs:
the CPU credits issue in section9-hq, and Petter's graph (about 15 h near 100%
led to a 40% limit; baseline 15-20%). Saito's #67 sets the figure.
