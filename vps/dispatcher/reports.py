"""The "Spend" and "Sync" issues the VPS keeps current, and `status` for the runbook."""

from decimal import Decimal
from pathlib import Path

from . import board, ledger, limits, net, openrouter
from .when import month_start, parse_when, utc_now

LEDGER_AGENTS = ("Batou", "Togusa", "Tachikoma", "Ishikawa")


def status(ctx):
    a, log = ctx.a, ctx.log
    todo = ctx.board.todo()
    log(f"queue: {len(todo)} todo ({', '.join('#%d' % i['number'] for i in todo[:10]) or 'none'})")
    ok, why = openrouter.spend_ok(a.openrouter, ctx.orkey)
    log("spend: ok" if ok else "spend: " + why)
    log(f"lanes: {limits.lanes_setting(a.limits)}")
    try:
        net.http("GET", a.hermes.rstrip("/") + "/v1/models", ctx.hermes_key, timeout=10)
        log("hermes: reachable")
    except net.Failure as e:     # any HTTP answer, even an error, means it is up
        log("hermes: " + ("NOT reachable (" + str(e) + ")" if "no response" in str(e) else "reachable"))


def month_by_agent(rows, now):
    """Agent -> this month's ledger total. The four ledger agents are always there, at 0.0."""
    start = month_start(now)
    per = {name: 0.0 for name in LEDGER_AGENTS}
    for r in rows:
        started = parse_when(r.get("start"))
        if started and started >= start:
            name = r.get("agent") or "unknown"
            per[name] = per.get(name, 0.0) + ledger.run_cost(r)
    return per


def reconcile_lines(rows, usage, now):
    """Spend-issue block that books every dollar of the key's month usage to an owner (Petter, 14 Nov):
    each agent's ledger total, plus "Hermes background" for what no run recorded."""
    money = ledger.money
    per = month_by_agent(rows, now)
    names = list(LEDGER_AGENTS) + sorted(n for n in per if n not in LEDGER_AGENTS)
    booked = sum(per.values())
    gap = float(usage) - booked
    background = max(gap, 0.0)
    over = " (ledger exceeds key — check two-lane overlap)" if gap < 0 else ""
    total = " (= key usage)" if gap >= 0 else f" (key usage {money(float(usage))})"
    return ["",
            f"- Booked to agents (ledger, this month): {money(booked)}",
            "  " + " · ".join(f"{n} {money(per[n])}" for n in names),
            f"- Hermes background (not tied to a run): {money(background)}" + over,
            f"- Owned total: {money(booked + background)}" + total]


def usd(k, name):
    """A /key figure as dollars, or n/a."""
    return f"${Decimal(str(k[name])):.2f}" if k.get(name) is not None else "n/a"


def cash(k, name, mult):
    """A /key figure in cash (usage times the root-only multiplier), or n/a."""
    if k.get(name) is None:
        return "n/a"
    return f"${Decimal(str(k[name])) * Decimal(str(mult)):.2f}"


def spend_lines(k, mult, now):
    """The OpenRouter block of the "Spend" issue, from the /key reply."""
    lines = [
        f"**Live snapshot, updated hourly by the VPS.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        "OpenRouter (the key the agents use; true cost):",
        f"- This month (UTC): {usd(k, 'usage_monthly')}",
        f"- Cap: {usd(k, 'limit')}" + (f" ({k['limit_reset']} reset)" if k.get("limit_reset") else ""),
        f"- Left: {usd(k, 'limit_remaining')}",
        f"- Today (UTC): {usd(k, 'usage_daily')}"]
    if mult != 1.0:
        lines.append(f"- Cash (x{mult:g} fees and tax): {cash(k, 'usage_monthly', mult)} this month, "
                     f"{cash(k, 'usage_daily', mult)} today. The cap stays usage, not cash.")
    return lines


def spend(ctx):
    """Keep one open issue titled "Spend" current: OpenRouter's own month, cap, left and today."""
    a = ctx.a
    k = (net.http("GET", a.openrouter.rstrip("/") + "/key", ctx.orkey, timeout=20) or {}).get("data") or {}
    if "usage_monthly" not in k:
        raise net.Failure("OpenRouter /key reply has no usage_monthly")
    mult = limits.cash_multiplier(a.limits)
    now = utc_now()
    lines = spend_lines(k, mult, now)
    if isinstance(k["usage_monthly"], (int, float)):
        lines += reconcile_lines(ledger.read_ledger(ledger.ledger_path(a)), k["usage_monthly"], now)
    lines += ["", "Rewritten every hour; don't edit. Leave this issue open and pinned."]
    board.keep_issue(ctx.board, ctx.log, "Spend", "\n".join(lines))


def health_lines(path):
    """The timer health block sync.sh gathered (root-owned file, plain text); empty without one."""
    if not path:
        return []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError:
        return []
    return ["**Timers** (as the VPS saw them, no SSH needed):", ""] + [net.redact(l) for l in text.splitlines()[:30]] + [""]


def note(ctx):
    """Keep one open issue titled "Sync" current: what the VPS did with itself. Rewrites the body."""
    a, log = ctx.a, ctx.log
    if a.state not in ("ok", "blocked"):
        raise net.Failure("note needs --state ok or --state blocked")
    now = utc_now()
    body = "\n".join([
        f"**VPS sync: {a.state}.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        a.message, ""] + health_lines(a.health_file) + [
        "Rewritten by `vps/sync.sh` on the VPS; don't edit. Leave this issue open."])
    labels = ["status:blocked"] if a.state == "blocked" else []
    open_ = ctx.board.call("GET", "/issues?state=open&sort=created&direction=asc&per_page=100") or []
    issue = next((i for i in board.issues_only(open_) if i["title"] == "Sync"), None)
    if issue:
        ctx.board.call("PATCH", f"/issues/{issue['number']}", {"body": body, "labels": labels})
        log(f"sync: #{issue['number']} {a.state}")
        return
    issue = ctx.board.call("POST", "/issues", {"title": "Sync", "body": body, "labels": labels})
    log(f"sync: opened #{issue['number']} ({a.state})")
