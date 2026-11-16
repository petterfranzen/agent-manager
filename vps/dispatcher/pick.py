"""Sweep, pick one task and claim it: the guards a task passes before it may run."""

import datetime as dt
import time
from pathlib import Path

from . import board as tasks
from . import cpu, lanes, ledger, limits, net, openrouter
from .when import utc_now

GRACE = 600                  # a working issue is stale this long after its agent's timeout


def budget_ok(a, agent, now=None):
    """(ok, why) for the root-only per-agent monthly ceiling. No [spend] entry for the agent: no guard."""
    cap = limits.spend_caps(a.limits).get(agent["name"])
    if cap is None:
        return True, ""
    now = now or utc_now()
    spent = ledger.month_spend(ledger.read_ledger(ledger.ledger_path(a)), agent["name"], now)
    if spent >= cap:
        return False, (f"Blocked by the dispatcher: {agent['name']} has used ${spent:.2f} of the "
                       f"${cap:.2f} monthly budget set for it in /etc/section9/limits.toml (from the run "
                       f"ledger, this UTC month). Raise the budget there or wait for the month to reset, "
                       f"then set `status:todo` again.")
    return True, ""


def day_ok(a, agent, now=None):
    """False when the agent already ran its day cap today (#205 item 6); the pick skips it.

    Counts the run ledger for the current UTC day, the same source the per-agent budget guard
    reads. It only skips -- it never blocks an issue -- so a task held by the cap runs tomorrow.
    """
    cap = limits.day_cap(agent)
    if cap <= 0:
        return True
    now = now or utc_now()
    return ledger.runs_on(ledger.read_ledger(ledger.ledger_path(a)), agent["name"], now) < cap


def recover_stale(board, agents, caps, log):
    """Block status:working issues whose run can't still be going. Called under the pick lock, so the two
    lanes never sweep at the same time; a run in the other lane may be live, but its claim just refreshed
    `updated_at`, so it stays under the limit. A dispatcher killed mid-run (reboot, OOM, the unit's
    TimeoutStartSec) leaves its issue `working`, and todo() would never look at it again. The age limit is
    the same ceiling the run itself got, so a run cut off by the root-only cap is swept at that cap, not the
    roster's longer timeout."""
    now = utc_now()
    for issue in board.working():
        agent = tasks.agent_of(issue, agents)
        limit = (limits.ceiling(agent, caps) if agent else limits.DEFAULT_TIMEOUT) + GRACE
        age = (now - dt.datetime.fromisoformat(issue["updated_at"])).total_seconds()
        if age <= limit:
            continue
        n = issue["number"]
        board.block(issue, f"Blocked by the dispatcher: `status:working` for {int(age // 60)} min with no update, "
                           f"past the {limit // 60} min limit; the run was probably killed. Set `status:todo` to try again.")
        log(f"#{n} blocked: stale working")


def spend_reasons(a, orkey, setting, log):
    """Why the month's spend holds low-priority work: ahead of the pace line, or unreadable (fail closed)."""
    now = utc_now()
    usage = openrouter.key_usage(a.openrouter, orkey)
    try:
        cap, spend = float(usage["limit"]), float(usage["usage_monthly"])
        p = openrouter.month_pace(now, cap, spend)
        if openrouter.ahead_of(p, spend, float(setting.get("slack_days", 1.0))):
            return [f"spend ${spend:.2f} is ahead of the ${p['linear_to_date']:.2f} pace line"]
    except (KeyError, TypeError, ValueError):
        log("pacing: spend figures unreadable; holding low-priority work (fail closed)")
        return ["spend figures unreadable"]
    return []


def steal_reasons(setting, log):
    """Why the CPU holds low-priority work: Hostinger's limit is on, or unreadable (fail closed)."""
    try:
        first = cpu.cpu_sample()
        time.sleep(float(setting.get("sample_seconds", 1.0)))
        d = cpu.cpu_delta(first, cpu.cpu_sample())
        limit = float(setting.get("steal_pct", 10.0))
        if d["steal_pct"] > limit:
            return [f"steal {d['steal_pct']:.1f}% is over {limit:.0f}% (Hostinger's limit is on)"]
    except (net.Failure, OSError, IndexError, KeyError, ValueError) as e:
        log(f"pacing: cannot read CPU ({e}); holding low-priority work (fail closed)")
        return ["CPU figures unreadable"]
    return []


def hold_low_priority(todo, a, orkey, log):
    """The pacing rule, on by default (#205 item 4); the root-only file only tunes it.

    Under the line: every task is admitted. Over the line, or with the CPU limit on: only
    `priority:low` issues wait; ordinary work still runs. `[pacing] enabled = false` turns it
    off. Unreadable figures fail closed: the low-priority work is held, never run blind.
    """
    kept = [t for t in todo if not tasks.has_priority(t[0], "low")]
    if len(kept) == len(todo):
        return todo                      # pacing holds only low-priority work; nothing to hold here
    setting = limits.pacing_settings(a.limits)
    if not setting.get("enabled"):
        return todo
    reasons = spend_reasons(a, orkey, setting, log) + steal_reasons(setting, log)
    if not reasons:
        return todo
    log(f"pacing: holding {len(todo) - len(kept)} low-priority task(s): {'; '.join(reasons)}")
    return kept


class Beside:
    """What the other lane is on, for the symmetric exclusion (#79, #100).

    With lanes = 2 even lane 0 keeps off the agent and repo the other lane is in. With one lane
    nothing is read and nothing changes. The exclusion stays on while the other lane holds its
    run lock, so flipping `lanes` back to 1 mid-run does not open the gap (#100)."""

    def __init__(self, ctx, agents, lane):
        a = ctx.a
        self.lane = lane
        self.two = (bool(lane) or limits.lanes_setting(a.limits) == 2
                    or lanes.lane_lock_held(Path(a.state_dir) / lanes.LANE_LOCK.format(1)))
        held = tasks.inflight(ctx.board, agents) if self.two else (set(), set(), False)
        self.agents, self.repos, self.unknown = held
        self.running = bool(self.agents or self.repos or self.unknown)   # the other lane is running something

    def conflicts(self, agent, key):
        """True when this lane must leave a task for `agent` in repo `key` alone this tick."""
        if not self.two:
            return False
        if agent["name"].lower() in self.agents:
            return True                     # the other lane's agent
        if self.lane:                       # lane 1: only a known repo no live run is in
            return self.unknown or not key or key in self.repos
        # lane 0 beside a live lane-1 run; an unknown repo conflicts with any
        return key in self.repos if key else self.running


class Queue:
    """One pass over the todo list: what this lane may run, and why the rest may not."""

    def __init__(self, ctx, agents, beside):
        self.ctx, self.agents, self.beside = ctx, agents, beside
        self.todo, self.unassignable, self.over = [], [], []
        self.offered, self.capped = 0, set()        # #205 item 6: who is already over today's run cap
        self.seen, self.day_of, self.budget_of = None, {}, {}

    def under_day_cap(self, pool):
        """The pool without the agents that ran their day cap; those are not chosen today (#205 item 6)."""
        for who in pool:
            if who["name"] not in self.day_of:
                self.day_of[who["name"]] = day_ok(self.ctx.a, who)
        self.capped.update(w["name"] for w in pool if not self.day_of[w["name"]])
        return [who for who in pool if self.day_of[who["name"]]]

    def idlest(self, pool):
        """The agent in the pool whose last board activity is oldest."""
        if self.seen is None and len(pool) > 1:
            self.seen = tasks.activity(self.ctx.board, self.agents)   # one extra read, only when a task has a pool to choose from
        return min(pool, key=lambda who: (self.seen or {}).get(who["name"], ""))

    def budget(self, agent):
        if agent["name"] not in self.budget_of:                                   # one ledger read per agent
            self.budget_of[agent["name"]] = budget_ok(self.ctx.a, agent)
        return self.budget_of[agent["name"]]

    def offer(self, issue):
        """File one todo issue: runnable, over budget, unassignable, or skipped this tick."""
        pool, why = tasks.candidates(issue, self.agents)
        if not pool and why != "no agent label":
            self.unassignable.append((issue, why))
        if not pool:
            return
        self.offered += 1
        pool = self.under_day_cap(pool)
        if pool:
            self.assign(issue, self.idlest(pool))

    def assign(self, issue, agent):
        key = tasks.repo_key(issue)
        if self.beside.conflicts(agent, key):
            return
        ok, why = self.budget(agent)
        if not ok:
            self.over.append((issue, agent, why))
            return
        pooled = not tasks.labelled(issue, self.agents)
        self.todo.append((issue, agent, pooled, key))

    def block_first_over(self):
        """The only work left is an agent over its monthly budget: say so, once."""
        self.over.sort(key=lambda t: tasks.priority_rank(t[0]))
        issue, agent, why = self.over[0]
        self.ctx.board.block(issue, why)
        self.ctx.log(f"#{issue['number']} blocked: {agent['name']} over budget")

    def block_first_unassignable(self):
        """E.g. only its own author could take it: tell the Major instead of looping on it."""
        issue, why = self.unassignable[0]
        self.ctx.board.block(issue, f"Blocked by the dispatcher: {why}. Relabel it for an agent who may "
                                    f"take it, then set `status:todo` again.")
        self.ctx.log(f"#{issue['number']} blocked: {why}")

    def explain_empty(self):
        """Nothing to run this tick: log why, and block the one issue that needs a human."""
        lane, log = self.beside.lane, self.ctx.log
        if self.capped:       # the only work left is agents that ran their day cap; wait, do not block
            log("day cap: " + ", ".join(sorted(self.capped)) + " at their runs-per-day cap today")
        if self.over:
            self.block_first_over()
        elif self.unassignable:
            self.block_first_unassignable()
        elif lane:
            log(f"lanes: lane {lane} found nothing it may run beside lane 0")


def admit(ctx, todo):
    """The runnable tasks in pick order, after the fail-closed spend check and the pacing rule.
    Empty when nothing may run; the reason is logged, and a spend refusal blocks the first task."""
    # Priority before age: a stable sort, so oldest-first survives within each priority group.
    todo.sort(key=lambda t: tasks.priority_rank(t[0]))
    ok, why = openrouter.spend_ok(ctx.a.openrouter, ctx.orkey)
    if not ok:
        issue = todo[0][0]
        ctx.board.block(issue, why)
        ctx.log(f"#{issue['number']} blocked: spend")
        return []
    kept = hold_low_priority(todo, ctx.a, ctx.orkey, ctx.log)
    if not kept:
        ctx.log("pacing: nothing left to admit")
    return kept


def claim(ctx, task, lane):
    """Set the task `status:working`, unless it stopped being todo since the list was read. Returns
    the issue as re-read, or None."""
    issue, agent, pooled, _key = task
    fresh = ctx.board.call("GET", f"/issues/{issue['number']}")   # cheap re-check: the list read above may be stale
    if "status:todo" not in {l["name"] for l in fresh["labels"]}:
        ctx.log(f"#{issue['number']} is no longer todo; nothing claimed this tick")
        return None
    ctx.board.set_status(fresh, "working", agent if pooled else None)
    ctx.log(f"#{issue['number']} -> {agent['name']}" + (f" (lane {lane})" if lane else ""))
    return fresh


def choose_and_claim(ctx, agents, caps, lane=0):
    """Sweep, pick one issue and claim it. Runs under the pick lock (run_one), so two lanes never
    claim the same issue and never sweep at the same time; the lock is released before the run.

    Lane 0 takes the oldest admissible task, as always. Lane 1 takes the oldest task it may run
    *beside* the other lane: a different agent, and a repo no live run is in. The repo is the
    `repo:<name>` label, else a known repo named in the title or body; a task with neither, or a
    live task with neither, stands lane 1 down (two of them could touch the same repo) -- #79.
    Lane 0 applies the same exclusion the other way: it skips a task whose agent or repo the other lane is
    running, and, while the other lane is running anything, a task that names no repo (#100).
    Returns None, or the claim: the issue, its agent, the run's timeout, how many tasks were on
    offer (`queue_before`) and how many low-priority ones pacing held (`held`)."""
    recover_stale(ctx.board, agents, caps, ctx.log)
    queue = Queue(ctx, agents, Beside(ctx, agents, lane))
    for issue in ctx.board.todo():
        queue.offer(issue)
    if not queue.todo:
        queue.explain_empty()
        return None
    admitted = len(queue.todo)             # what this lane may take, before the pacing rule
    todo = admit(ctx, queue.todo)
    fresh = claim(ctx, todo[0], lane) if todo else None
    if fresh is None:
        return None
    agent = todo[0][1]
    return {"issue": fresh, "agent": agent, "timeout": limits.ceiling(agent, caps),
            "queue_before": queue.offered, "held": admitted - len(todo)}
