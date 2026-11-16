"""One dispatcher tick in one lane: claim a task, run it through Hermes, record it in the ledger."""

import fcntl
from pathlib import Path

from . import cpu, lanes, ledger, limits, net, openrouter, pick
from .when import utc_now

PROVIDER = "openrouter"                          # every model is reached through OpenRouter only


def chat_body(model, system, user):
    """The request body. With no override (the resolved model is the default) it is exactly the old
    one: model "hermes-agent", no provider key. Only an allow-listed non-default model adds its id
    and `provider` (#101)."""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    if model == limits.DEFAULT_MODEL:
        return {"model": "hermes-agent", "messages": messages}
    return {"model": model, "provider": PROVIDER, "messages": messages}


def named(value):
    """A model name worth booking: the stripped string, or '' for anything else."""
    return value.strip() if isinstance(value, str) else ""


def served_model(reply, requested):
    """The model a run is booked to: the served runtime when the gateway reports one, else the
    reply's model field, else what we asked for. The runtime is the pair that actually served the
    turn, so a fallback switch is booked to the fallback and not to the model we requested (#101)."""
    if not isinstance(reply, dict):
        return requested
    runtime = reply.get("runtime")
    served = named(runtime.get("model")) if isinstance(runtime, dict) else ""
    if served:
        return served
    echoed = named(reply.get("model"))
    if echoed not in ("", "hermes-agent"):      # the gateway's own alias says nothing about the model
        return echoed
    return requested


def prompt_for(agent, issue, preamble="", briefs_dir=None):
    """(system, user): the guardrail preamble, the agent's brief, then the task."""
    locked = Path(briefs_dir) / f"{agent['name']}.md" if briefs_dir else None
    source = locked if locked is not None and locked.exists() else Path(agent["_root"]) / agent["brief"]
    brief = source.read_text(encoding="utf-8")
    if preamble:
        brief = preamble.strip() + "\n\n---\n\n" + brief
    name, n = agent["name"], issue["number"]
    return brief, (
        f"Task #{n}: {issue['title']}\n{issue['html_url']}\n\n{issue.get('body') or '(no description)'}\n\n"
        f"You are {name}. Before you finish, report with "
        f"`s9-task --as {name} status {n} <review|done|blocked> \"message\"`."
    )


def tail(text, n=20):
    return "\n".join((text or "").strip().splitlines()[-n:]) or "(no output)"


def sample_or_none():
    try:
        return cpu.cpu_sample()
    except (net.Failure, OSError, IndexError):
        return None


class Meter:
    """The clock, OpenRouter's counters and the CPU sample around one run, for its ledger record."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.started = utc_now()
        self.usage_before = openrouter.key_usage(ctx.a.openrouter, ctx.orkey)
        self.cpu_before = sample_or_none()
        self.ended, self.usage_after = None, None
        self.cpu, self.cpu_after = None, None

    def stop(self):
        """The run has ended."""
        self.ended = utc_now()

    def read_after(self):
        """The counters and the CPU once the run's outcome is on the board."""
        self.usage_after = openrouter.key_usage(self.ctx.a.openrouter, self.ctx.orkey)
        if self.cpu_before is None:
            return
        try:
            after = cpu.cpu_sample()
            self.cpu, self.cpu_after = cpu.cpu_delta(self.cpu_before, after), after
        except (net.Failure, OSError, IndexError):
            self.cpu, self.cpu_after = None, None

    def fields(self):
        return {
            "start": self.started.isoformat(timespec="seconds"), "end": self.ended.isoformat(timespec="seconds"),
            "seconds": round(max((self.ended - self.started).total_seconds(), 0.0), 3),
            "usage_before": self.usage_before, "usage_after": self.usage_after,
            "cost_usd": ledger.money_delta(self.usage_before, self.usage_after),
            "cpu": self.cpu, "cpu_before": self.cpu_before, "cpu_after": self.cpu_after,
        }


def ask(request, timeout):
    """Run the task through Hermes. Returns (reply, output, problem); on a failure the output is
    whatever the run had written when its time ran out."""
    try:
        reply, _ = net.chat_stream(request, timeout)
    except net.Failure as e:
        return None, e.partial or "", str(e)
    out = (((reply or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return reply, out, ""


def failed_outcome(problem):
    if not problem:
        return "no-output"
    return "timeout" if "timed out" in problem.lower() else "blocked"


def settle(ctx, n, problem, out):
    """The run's outcome, from the issue as it is now: the agent reports with s9-task, which changes
    the labels. A failed run, or one that left the issue `working`, is blocked with its last lines."""
    now = ctx.board.call("GET", f"/issues/{n}")
    labels = {l["name"] for l in now["labels"]}
    if not problem and "status:working" not in labels:
        ctx.log(f"#{n} finished")
        return "blocked" if "status:blocked" in labels else "ok"
    ctx.board.block(now, f"Blocked by the dispatcher: {problem or 'the run ended without a final status'}.\n\n"
                         f"Partial output so far:\n```\n{tail(out)}\n```\n"
                         f"Set `status:todo` to try again.")
    ctx.log(f"#{n} blocked: {problem or 'no final status'}")
    return failed_outcome(problem)


def tokens_of(reply):
    """The numeric counters of the reply's `usage`, or None."""
    if isinstance(reply, dict) and isinstance(reply.get("usage"), dict):
        return {k: v for k, v in reply["usage"].items() if isinstance(v, (int, float))}
    return None


def record_of(claim, lane, answer):
    """What the ledger says about the task and the reply; `answer` is (reply, requested model, outcome)."""
    reply, model, outcome = answer
    issue, agent = claim["issue"], claim["agent"]
    return {
        "v": ledger.LEDGER_VERSION, "issue": issue["number"], "type": ledger.task_type(issue.get("title")),
        "agent": agent["name"], "role": agent.get("role", ""),
        "model": served_model(reply, model), "model_requested": model,
        "outcome": outcome, "lane": lane, "queue_before": claim["queue_before"],
        "held_low_priority": claim["held"], "tokens": tokens_of(reply),
    }


def claim_one(ctx, agents, caps, lane):
    """Choose and claim under the pick lock, so one lane chooses at a time. None: nothing this tick."""
    with open(Path(ctx.a.state_dir) / lanes.PICK_LOCK, "w") as lock:
        if not lanes.try_lock(lock):
            ctx.log("lanes: the other lane is choosing; nothing this tick")
            return None
        try:
            return pick.choose_and_claim(ctx, agents, caps, lane)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def run_one(ctx, lane=0):
    a = ctx.a
    agents, _ = limits.load_roster(a.roster)
    caps, preamble = limits.limits_of(a.limits)
    for ag in agents:
        ag["_root"] = a.repo_root
    claim = claim_one(ctx, agents, caps, lane)
    if claim is None:
        return
    issue, agent = claim["issue"], claim["agent"]
    n = issue["number"]
    model = limits.model_for(agent, *limits.models_of(a.limits), ctx.log)   # root-only allow-list gates a repo's choice
    system, user = prompt_for(agent, issue, preamble, Path(a.secrets_dir) / "briefs")
    meter = Meter(ctx)
    request = net.chat_request(a.hermes.rstrip("/") + "/v1/chat/completions", ctx.hermes_key,
                               chat_body(model, system, user), {"X-Hermes-Session-Id": f"issue-{n}"})
    reply, out, problem = ask(request, claim["timeout"])
    meter.stop()
    outcome = settle(ctx, n, problem, out)
    meter.read_after()
    try:                                    # building and writing the record: neither may stop a run
        record = {**record_of(claim, lane, (reply, model, outcome)), **meter.fields()}
        ledger.append_ledger(Path(a.state_dir) / ledger.LEDGER_NAME, record)
    except Exception as e:                  # a ledger must never stop or fail a run
        ctx.log(f"ledger: not written for #{n} ({e})")


def run_lane(ctx):
    """The default command: one run in this lane, unless the lane is off, busy or stood down."""
    a = ctx.a
    Path(a.state_dir).mkdir(parents=True, exist_ok=True)
    if a.lane >= limits.lanes_setting(a.limits):
        return              # quiet: the lane-1 timer fires every 2 min, its "off" line is pure noise (#78 nit 2)
    with open(Path(a.state_dir) / lanes.LANE_LOCK.format(a.lane), "w") as lock:
        if not lanes.try_lock(lock):
            ctx.log(f"another run is in progress in lane {a.lane}")
            return
        if a.lane and not second_lane_may_run(ctx):
            return
        run_one(ctx, a.lane)


def second_lane_may_run(ctx):
    """The second lane decides for itself, and stands down."""
    allowed, why = lanes.lane_state(ctx.a, ctx.log)
    if allowed < 2:
        ctx.log(f"lanes: lane {ctx.a.lane} stands down: {why}")
    return allowed >= 2
