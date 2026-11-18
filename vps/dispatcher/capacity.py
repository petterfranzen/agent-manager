"""The "Capacity" issue: the run ledger rolled up, against the month's pace line."""

import datetime as dt
from pathlib import Path

from . import board, cpu, ledger, limits, openrouter
from .ledger import money
from .when import month_start, utc_now

DASH = "—"


def is_number(value):
    return isinstance(value, (int, float))


def spent(s):
    """A bucket's spend, or a dash when no run in it could be priced."""
    return money(s["cost"]) if s["priced"] else DASH


def rate(hours, cost, priced):
    """$/hour only when the sample is long enough to mean something (72 s)."""
    return f"${cost / hours:.2f}" if hours >= 0.02 and priced else DASH


def table(rows, label):
    lines = [f"| {label} | runs | busy h | $ | $/h | timeouts | timeout h | timeout $ | credits |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for name, group in rows:
        s = ledger.summarise(group)
        lines.append(f"| {name} | {s['runs']} | {s['hours']:.2f} | {spent(s)} | "
                     f"{rate(s['hours'], s['cost'], s['priced'])} | {s['timeouts']} | "
                     f"{s['timeout_seconds'] / 3600.0:.2f} | {money(s['timeout_cost'])} | {s['credits']:.0f} |")
    return lines


def share(part, whole):
    return f"{100.0 * part / whole:.0f}%" if whole else DASH


def timeout_lines(week):
    """Every task that timed out this week, worst first: the jobs that cost money and never finished.

    A task with two or more timeouts is the shape to act on (split it or fix what stalls it).
    """
    hit = grouped([r for r in week if r.get("outcome") == "timeout"], lambda r: r.get("issue") or 0)
    if not hit:
        return ["**Timed out, last 7 days:** none.", ""]
    hit.sort(key=lambda item: (-len(item[1]), -sum(ledger.run_cost(r) for r in item[1])))
    lines = ["**Timed out, last 7 days** (tasks with 2 or more timeouts first)",
             "| Issue | agent | timeouts | minutes | $ |", "|---|---|---:|---:|---:|"]
    for issue, runs in hit[:15]:
        agents = ", ".join(sorted({r.get("agent") or "unknown" for r in runs}))
        minutes = sum(float(r.get("seconds") or 0.0) for r in runs) / 60.0
        lines.append(f"| #{issue} | {agents} | {len(runs)} | {minutes:.0f} | "
                     f"{money(sum(ledger.run_cost(r) for r in runs))} |")
    if len(hit) > 15:
        lines.append(f"| … {len(hit) - 15} more | | | | |")
    return lines + [""]


def lane_line(rows):
    """Runs and busy hours per lane, and the hours two ran at once. A row without a `lane` is one of
    the single-lane runs from before the switch existed: lane 0."""
    by_lane = grouped(rows, lambda r: int(r.get("lane") or 0))
    if not by_lane:
        return DASH
    parts = [f"lane {n}: {len(g)} run(s), {ledger.summarise(g)['hours']:.2f} h" for n, g in by_lane]
    return "; ".join(parts) + f"; two at once {ledger.concurrent_hours(rows):.2f} h"


def grouped(rows, key):
    """[(key, rows)] sorted by key."""
    groups = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    return sorted(groups.items())


def cash_line(usage, mult):
    """The same figures in cash: usage times the root-only multiplier."""
    cap, spend, today = usage.get("limit"), usage.get("usage_monthly"), usage.get("usage_daily")
    cash_today = money(float(today) * mult) if is_number(today) else DASH
    return (f"- **Cash (x{mult:g} fees and tax):** {money(float(spend) * mult)} of "
            f"{money(float(cap) * mult)} used; today {cash_today}. The cap and the fail-closed "
            "check still use the usage figures above.")


def pace_lines(usage, p, mult):
    """The pace block; `p` is the month's pace line, or None when the key's figures could not be read."""
    if p is None:
        return ["**Pace:** OpenRouter's cap or month-to-date usage could not be read; no pace line this run.", ""]
    cap, spend = usage.get("limit"), usage.get("usage_monthly")
    ahead = float(spend) - p["linear_to_date"]
    actual = "ahead by " + money(ahead) if ahead >= 0 else "behind by " + money(-ahead)
    times = " (" + format(float(spend) / p["linear_to_date"], ".1f") + "x)" if p["linear_to_date"] else ""
    lines = [f"**Pace:** {money(spend)} of {money(cap)} used; day {p['elapsed']:.1f} of {p['days']}.",
             f"- Fixed linear pace: {money(p['linear_per_day'])}/day, so {money(p['linear_to_date'])} "
             f"by now. Actual is {actual}{times}.",
             f"- Remaining-days pace (after the ${openrouter.RESERVE:.0f} reserve): {money(p['remaining_per_day'])}/day "
             f"for {p['left_days']:.1f} days, from {money(float(cap) - float(spend))} left.",
             f"- Today (UTC): {money(usage.get('usage_daily'))} used, fixed pace {money(p['linear_per_day'])}."]
    if mult != 1.0:
        lines.append(cash_line(usage, mult))
    return lines + [""]


def key_line(spend, ms):
    """The key's month against what the ledger booked."""
    booked = f"{spent(ms)} over {ms['runs']} run(s) ({ms['priced']} priced)"
    if not is_number(spend):
        return f"- Key (OpenRouter `/key`), month to date: could not be read. The ledger books {booked}."
    gap = float(spend) - ms["cost"]
    return (f"- Key (OpenRouter `/key`), month to date: {money(spend)}. The ledger books {booked}, "
            f"{money(abs(gap))} " + ("unattached to any run." if gap >= 0 else "more than the runs booked."))


def priced_note(ws):
    """How many of the week's runs the spend figure rests on."""
    if not ws["priced"]:
        return " (no run had a readable cost delta)"
    return " (from " + str(ws["priced"]) + (" priced run)" if ws["priced"] == 1 else " priced runs)")


def week_lines(week):
    ws = ledger.summarise(week)
    guidance = cpu.SAFE_CREDITS_DAY * 7
    return ["**Last 7 days**",
            f"- Runs: {ws['runs']}; busy: {ws['hours']:.2f} h; spend: {spent(ws)}{priced_note(ws)}.",
            f"- Timeouts: {ws['timeouts']} of {ws['runs']} runs ({share(ws['timeouts'], ws['runs'])}; "
            f"{ws['timeout_seconds'] / 3600.0:.2f} h and {money(ws['timeout_cost'])} wasted); outcomes: "
            + ", ".join(f"{k} {v}" for k, v in ws["outcomes"].items()) + ".",
            f"- Queue idle (upper bound): {ledger.queue_idle_hours(week):.2f} h.",
            f"- CPU credits: {ws['credits']:.0f} against a {guidance:.0f} guidance for 7 days "
            f"({100.0 * ws['credits'] / guidance:.0f}%).",
            f"- Lanes: {lane_line(week)}.", ""]


def month_credits(ms, p):
    """The month's CPU credits, against the guidance for the days so far when the pace line is known."""
    if p is None:
        return f"- CPU credits: {ms['credits']:.0f}."
    days = max(p["elapsed"], 1)
    return (f"- CPU credits: {ms['credits']:.0f} against a {cpu.SAFE_CREDITS_DAY * days:.0f} "
            f"guidance for {days:.1f} days "
            f"({100.0 * ms['credits'] / (cpu.SAFE_CREDITS_DAY * days):.0f}%).")


def month_lines(mtd, spend, p):
    ms = ledger.summarise(mtd)
    return ["**Month to date**",
            f"- Runs: {ms['runs']}; busy: {ms['hours']:.2f} h; spend: {spent(ms)}.",
            key_line(spend, ms),
            f"- Timeouts: {ms['timeouts']} of {ms['runs']} runs ({share(ms['timeouts'], ms['runs'])}; "
            f"{ms['timeout_seconds'] / 3600.0:.2f} h and {money(ms['timeout_cost'])} wasted); "
            f"queue idle (upper bound): {ledger.queue_idle_hours(mtd):.2f} h.",
            month_credits(ms, p), ""]


def breakdown_lines(week):
    """The week by agent, by agent and model, and by task type."""
    by_agent = grouped(week, lambda r: r.get("agent") or "unknown")
    by_agent_model = grouped(week, lambda r: f"{r.get('agent') or 'unknown'} · {r.get('model') or 'unknown'}")
    by_type = grouped(week, lambda r: r.get("type") or "task")
    return (["**Last 7 days by agent**"] + table(by_agent, "Agent") + [""]
            + ["**Last 7 days by agent and model**"] + table(by_agent_model, "Agent · model") + [""]
            + ["**Last 7 days by task type**"] + table(by_type, "Type") + [""])


def gap_lines(rows, a):
    """What the ledger cannot say: runs without a cost figure, or no runs at all."""
    lines = []
    stale = [r for r in rows if not ledger.priceable(r)]
    if stale:
        lines += [f"- {len(stale)} run(s) have no cost figure: the key delta was absent and the run "
                  "carried no tokens to price it from. Nothing was guessed; collect the next run's "
                  "`usage_before`/`usage_after` and its `tokens`.", ""]
    if not rows:
        lines += [f"No ledger data yet. The first run appends to `{Path(a.state_dir) / ledger.LEDGER_NAME}`.", ""]
    return lines


def capacity(ctx):
    """Keep one open issue titled "Capacity" current: the ledger rolled up, and the pace line."""
    a = ctx.a
    now = utc_now()
    usage = openrouter.key_usage(a.openrouter, ctx.orkey)
    rows = ledger.read_ledger(ledger.ledger_path(a))
    week, mtd = ledger.since(rows, now - dt.timedelta(days=7)), ledger.since(rows, month_start(now))
    cap, spend = usage.get("limit"), usage.get("usage_monthly")
    readable = is_number(cap) and is_number(spend)
    p = openrouter.month_pace(now, cap, spend) if readable else None
    mult = limits.cash_multiplier(a.limits) if readable else 1.0
    lines = [f"**Capacity, rolled up by the VPS from the run ledger.** As of {now:%Y-%m-%d %H:%M} UTC.", ""]
    lines += pace_lines(usage, p, mult) + week_lines(week) + month_lines(mtd, spend, p)
    lines += timeout_lines(week) + breakdown_lines(week) + gap_lines(rows, a)
    lines += ["Rewritten by `vps/dispatch.py capacity`; don't edit. Leave this issue open and pinned."]
    board.keep_issue(ctx.board, ctx.log, "Capacity", "\n".join(lines))
