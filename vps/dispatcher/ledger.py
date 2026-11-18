"""The run ledger: one JSON line per run under --state-dir (never in the repo), and what a run cost."""

import datetime as dt
import json
import os
import re
from pathlib import Path

from .when import day_start, month_start, parse_when

LEDGER_NAME = "runs.jsonl"   # one JSON line per run, under --state-dir (never in the repo)
LEDGER_VERSION = 1
TYPE_PREFIXES = {"qa", "review", "design", "research", "plan", "check", "sweep", "build",
                 "triage", "fix", "nightly", "report", "audit"}
# USD per million tokens (in, out) for the models an allow-list may name. Used only to price a run
# whose measured cost the OpenRouter counter could not give; an unknown model is priced at the
# dearest allowed rate, so a per-agent guard over-counts rather than under-counts.
PRICES = {
    "deepseek/deepseek-v4.1-flash": (0.05, 0.19),
    "z-ai/glm-5.3-flash": (0.15, 0.50),
    "minimax/minimax-m3": (0.30, 1.20),
    "anthropic/claude-haiku-5.5": (0.10, 0.50),
    "anthropic/claude-sonnet-5.5": (2.0, 10.0),
    "moonshotai/kimi-k3.1-pro": (2.0, 10.0),
}
DEAREST_PRICE = (2.0, 10.0)
OUTCOMES = ("ok", "timeout", "blocked", "no-output")


def ledger_path(a):
    """--ledger when given, else <state-dir>/runs.jsonl."""
    return Path(getattr(a, "ledger", "") or (Path(a.state_dir) / LEDGER_NAME))


def append_ledger(path, record):
    """Append one JSON line, mode 0640 in a 0750 dir. Raises; the caller decides that a ledger
    failure must not matter. The record holds every run's cost and CPU draw: not world-readable."""
    p = Path(path)
    p.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    try:
        os.fchmod(fd, 0o640)     # the mode above applies only when the file is created: fix an older, wider file
    except OSError:
        pass                     # best effort: a chmod that fails must not stop the run
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def as_row(line):
    """One ledger line as a record, or None for a corrupt line or one without a start."""
    try:
        row = json.loads(line)
    except ValueError:
        return None
    return row if isinstance(row, dict) and row.get("start") else None


def read_ledger(path):
    """Every good record; a corrupt line is skipped rather than losing the whole report."""
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    rows = [as_row(line) for line in lines]
    return [row for row in rows if row is not None]


def since(rows, start):
    """The rows whose run started at or after `start`."""
    return [r for r in rows if parse_when(r.get("start")) and parse_when(r["start"]) >= start]


def of_agent(rows, agent):
    return [r for r in rows if (r.get("agent") or "") == agent]


def price_from_tokens(row):
    """$ for one ledger row from its token counts at the model's rate, or None when it has none."""
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    if not tokens:
        return None
    pin, pout = PRICES.get(row.get("model") or "", DEAREST_PRICE)
    try:
        return (float(tokens.get("prompt_tokens") or 0) * pin
                + float(tokens.get("completion_tokens") or 0) * pout) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def measured(row):
    """The row's OpenRouter key delta when it is a number other than 0, else None."""
    delta = row.get("cost_usd")
    return delta if isinstance(delta, (int, float)) and delta != 0 else None


def priceable(row):
    """True when the row carries a cost we can book: a non-zero key delta, or tokens to price.

    A numeric $0 delta is not a free run. OpenRouter's counter is coarse, so a short run can start
    and end inside one counter step and book $0 while it still spent; such a row is priced from its
    tokens instead (run_cost) and so counts here.
    """
    return measured(row) is not None or price_from_tokens(row) is not None


def run_cost(row):
    """One ledger row's cost: the measured OpenRouter key delta, else an estimate from its tokens.

    The measured delta is exact but null -- or 0, when the counter did not move across the run -- so
    a zero delta falls back to the token estimate. That keeps a per-agent guard and the Capacity
    roll-up from drifting open over a string of cheap turns the counter booked as free.
    """
    delta = measured(row)
    if delta is not None:
        return float(delta)
    estimate = price_from_tokens(row)
    return estimate if estimate is not None else 0.0


def month_spend(rows, agent, now):
    """Measured $ for one agent in the current UTC month, from the ledger."""
    return sum(run_cost(r) for r in since(of_agent(rows, agent), month_start(now)))


def runs_on(rows, agent, now):
    """How many ledger runs this agent started today (UTC). The day cap's source, #205 item 6."""
    return len(since(of_agent(rows, agent), day_start(now)))


def money_delta(before, after, name="usage_monthly"):
    """Cost of one run from two /key snapshots, or None when the counter did not move/read."""
    try:
        a, b = before.get(name), after.get(name)
        if a is None or b is None:
            return None
        change = float(b) - float(a)
        return round(change, 6) if change >= 0 else None
    except (TypeError, ValueError):
        return None


def money(value):
    return f"${value:.2f}" if isinstance(value, (int, float)) else "—"


def task_type(title):
    """A coarse task type from the title's prefix ('QA: ...', 'Research: ...'), for grouping only."""
    m = re.match(r"\s*([A-Za-z]+)\s*:", title or "")
    word = m.group(1).lower() if m else ""
    return word if word in TYPE_PREFIXES else "task"


def add_run(out, row):
    """Add one ledger row to a `summarise` bucket."""
    seconds = float(row.get("seconds") or 0.0)
    status = row.get("outcome")
    cost = run_cost(row)
    out["seconds"] += seconds
    out["cost"] += cost
    out["priced"] += 1 if priceable(row) else 0
    out["credits"] += float((row.get("cpu") or {}).get("credits") or 0.0)
    if status in out["outcomes"]:
        out["outcomes"][status] += 1
    if status == "timeout":
        out["timeouts"] += 1
        out["timeout_seconds"] += seconds
        out["timeout_cost"] += cost


def summarise(rows):
    """One bucket of ledger rows: runs, busy time, money, failures and CPU credits."""
    out = {"runs": len(rows), "seconds": 0.0, "cost": 0.0, "priced": 0,
           "timeouts": 0, "timeout_seconds": 0.0, "timeout_cost": 0.0, "credits": 0.0,
           "outcomes": {name: 0 for name in OUTCOMES}}
    for row in rows:
        add_run(out, row)
    hours = out["seconds"] / 3600.0
    out["hours"] = hours
    out["per_hour"] = (out["cost"] / hours) if hours > 0 and out["priced"] else None
    return out


def queue_idle_hours(rows):
    """Upper bound on hours with work waiting and no run: the gap before a run that saw a queue.

    The ledger records only runs, so this counts a gap when the next run started with more
    than its own issue waiting. It can overstate (the task may have appeared mid-gap) and
    never includes time before the first recorded run.
    """
    ordered = sorted((r for r in rows if parse_when(r.get("start")) and parse_when(r.get("end"))),
                     key=lambda r: r["start"])
    idle = 0.0
    for prev, nxt in zip(ordered, ordered[1:]):
        if int(nxt.get("queue_before") or 0) > 1:
            idle += max((parse_when(nxt["start"]) - parse_when(prev["end"])).total_seconds(), 0.0) / 3600.0
    return idle


def ledger_credits(rows, now, hours=24.0):
    """CPU credits above the baseline that the runs of the last `hours` drew, from the ledger.

    Per-run figures, so with two lanes each counts some of the other's draw: the sum over-estimates,
    which is the safe direction for a fallback."""
    recent = since(rows, now - dt.timedelta(hours=hours))
    return sum(float((r.get("cpu") or {}).get("credits") or 0.0) for r in recent)


def run_span(row):
    """(start, end) of a run that took time, else None."""
    start, end = parse_when(row.get("start")), parse_when(row.get("end"))
    if start and end:
        return (start, end) if end > start else None
    return None


def concurrent_hours(rows):
    """Hours with two or more runs in flight at once: how much the second lane was actually used."""
    spans = [span for span in map(run_span, rows) if span]
    events = sorted([(start, 1) for start, _ in spans] + [(end, -1) for _, end in spans])
    # at equal times the end (-1) sorts first: no zero-length overlap
    live, hours, last = 0, 0.0, None
    for at, delta in events:
        if live >= 2:
            hours += (at - last).total_seconds() / 3600.0
        live += delta
        last = at
    return hours
