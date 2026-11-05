#!/usr/bin/env python3
"""Section 9 dispatcher: GitHub issues in section9-hq in, Hermes runs out.

  dispatch.py            run the oldest `agent:<name>` + `status:todo` issue
  dispatch.py --lane 1   the optional second lane (root-only limits.toml: lanes = 2)
  dispatch.py routines   open the issues for routines whose cron time has come
  dispatch.py status     queue, spend and Hermes reachability (for the runbook)
  dispatch.py spend      update the open "Spend" issue with OpenRouter's figures (hourly)
  dispatch.py cpu        update the open "CPU credits" issue with this VPS's own CPU figures (hourly)
  dispatch.py capacity   roll the run ledger up into the open "Capacity" issue (daily)
  dispatch.py note       update the open "Sync" issue: what the VPS did with itself

Run by systemd timers on the VPS (vps/systemd). One run at a time in each lane (lock files).
Standard library only. Secrets come from root-only files and are never printed:
  OPENROUTER_API_KEY, API_SERVER_KEY  in <secrets-dir>/hermes.env
  GH_TOKEN                            in <secrets-dir>/sandbox.env
Optional, root-only, outside the repo (a commit to it cannot change them):
  <secrets-dir>/limits.toml           per-agent run ceiling and the guardrail preamble
  <secrets-dir>/briefs/<Name>.md      an agent's brief, when it must not live in the repo
"""

import argparse
import calendar
import datetime as dt
import fcntl
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
REPO = "petterfranzen/section9-hq"
RESERVE = Decimal("1.00")   # stop this far below OpenRouter's cap (the company budget is $19 of $20)
KEYS = re.compile(r"(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
STATUSES = ("todo", "working", "review", "done", "blocked")
DEFAULT_TIMEOUT = 3600       # an agent without timeout_sec in the roster
GRACE = 600                  # a working issue is stale this long after its agent's timeout
SAMPLE_SECONDS = 5.0         # window for the /proc/stat sample behind the CPU credits snapshot
PROBE_SECONDS = 1.0          # length of the throttling probe; ~18 credits a day at this size
BASELINE = 0.25              # share of one vCPU a Hostinger VPS uses without spending credits
BALANCE_MAX = 8100.0         # core-seconds in a full balance: 3 h x 3600 x (1 vCPU - 25%)
SAFE_CREDITS_DAY = 8100.0    # one full balance a day is the guidance the Capacity issue measures against
CLK_TCK = float(os.sysconf("SC_CLK_TCK"))   # /proc/stat counts in USER_HZ ticks, not seconds
LEDGER_NAME = "runs.jsonl"   # one JSON line per run, under --state-dir (never in the repo)
LEDGER_VERSION = 1
LANE_STATE = "lanes.json"         # the lane fallback latch, under --state-dir
PICK_LOCK = "dispatch.pick.lock"  # one lane chooses at a time; held only while choosing
DEFAULT_QUIET_HOURS = 3.0        # safe readings this long before the second lane comes back
SAFE_STEAL_PCT = 10.0            # above this Hostinger's limit is on (the [pacing] default)
# The repos a task may name in its title or body when it carries no `repo:<name>` label.
# Anything else is unknown, and an unknown task only ever runs in lane 0.
REPOS = ("agent-manager", "section9-hq", "ghost-in-the-machine", "flight-tracker",
         "nvim-trainer", "dinner-planner", "portfolio")
TYPE_PREFIXES = {"qa", "review", "design", "research", "plan", "check", "sweep", "build",
                 "triage", "fix", "nightly", "report", "audit"}
SANDBOX_CORES, HERMES_CORES = 0.4, 0.3    # keep in step with vps/compose.yaml
PER_PAGE, MAX_PAGES = 100, 20     # board reads follow Link: rel="next" up to this; more than that is refused


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    value = None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    for line in text.splitlines():
        line = line.strip().removeprefix("export ").lstrip()
        key, sep, val = line.partition("=")
        if sep and not line.startswith("#") and key.strip() == name:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            value = val
    if not value:
        raise Failure(f"{name} is missing or empty in {path}")
    return value


def request(method, url, token, body=None, timeout=30, headers=None):
    """(parsed JSON or None, response headers)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "section9-dispatch")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, resp_headers = resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        raise Failure(f"{method} {redact(url)} -> {e.code}: {redact(e.read().decode('utf-8', 'replace'))[:300]}") from None
    except TimeoutError:
        raise Failure(f"{method} {redact(url)} -> timed out after {timeout} s") from None
    except (urllib.error.URLError, OSError) as e:
        raise Failure(f"{method} {redact(url)} -> no response: {redact(str(getattr(e, 'reason', e)))}") from None
    try:
        return (json.loads(raw) if raw.strip() else None), resp_headers
    except ValueError:
        raise Failure(f"{method} {redact(url)} -> reply is not JSON") from None


def http(method, url, token, body=None, timeout=30, headers=None):
    return request(method, url, token, body, timeout, headers)[0]


def next_link(headers):
    """The URL of Link: rel="next", or None."""
    for part in (headers.get("Link") or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;.*\brel="?next"?', part)
        if m:
            return m.group(1)
    return None


class Board:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/"), token

    def call(self, method, path, body=None):
        return http(method, f"{self.api}/repos/{REPO}{path}", self.token, body)

    def pages(self, path):
        """Every item of a list endpoint, following Link: rel="next". Refuses to go on past MAX_PAGES pages
        (a silently short list would hide the oldest todos), or to send the token to another host."""
        url = f"{self.api}/repos/{REPO}{path}{'&' if '?' in path else '?'}per_page={PER_PAGE}"
        items = []
        for _ in range(MAX_PAGES):
            page, headers = request("GET", url, self.token)
            items += page or []
            url = next_link(headers)
            if not url:
                return items
            if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(self.api)[:2]:
                raise Failure(f"GET {path} -> the next page is on another host; refusing to send the token there")
        raise Failure(f"GET {path} -> more than {MAX_PAGES * PER_PAGE} items; refusing to work from a partial list")

    def todo(self):
        """Open status:todo issues (not PRs), oldest first."""
        issues = self.pages("/issues?labels=status%3Atodo&state=open&sort=created&direction=asc")
        return [i for i in issues if "pull_request" not in i]

    def working(self):
        """Open status:working issues (not PRs)."""
        return [i for i in self.pages("/issues?labels=status%3Aworking&state=open") if "pull_request" not in i]

    def comment(self, n, text):
        self.call("POST", f"/issues/{n}/comments", {"body": redact(text)[:60000]})

    def set_status(self, issue, status, agent=None):
        labels = [l["name"] for l in issue["labels"] if not l["name"].startswith("status:")]
        if agent is not None:      # a pooled task becomes that agent's own, so a killed run is swept with its timeout
            labels = [x for x in labels if not x.lower().startswith("agent:")] + ["agent:" + agent["name"].lower()]
        self.call("PATCH", f"/issues/{issue['number']}", {"labels": labels + ["status:" + status]})


def label_names(issue):
    return {l["name"].lower() for l in issue["labels"]}


def agent_of(issue, agents):
    names = label_names(issue)
    return next((a for a in agents if f"agent:{a['name'].lower()}" in names), None)


ANY = "any"
AUTHOR = re.compile(r"(?im)^\s*author:\s*([A-Za-z][\w.-]*)")


def author_of(issue):
    """Who wrote the thing under review: an `author:<name>` label, else an `Author: <Name>` line in the body."""
    for name in sorted(label_names(issue)):
        if name.startswith("author:") and name[len("author:"):].strip():
            return name[len("author:"):].strip().lower()
    m = AUTHOR.search(issue.get("body") or "")
    return m.group(1).strip().lower() if m else None


def role_matches(agent, token):
    """`agent:<role>`: a word in the agent's role or title. `agent:qa` reaches Togusa."""
    words = ((agent.get("role") or "") + " " + (agent.get("title") or "")).lower().split()
    return token in words


def candidates(issue, agents):
    """(agents who may take it, why not). An exact `agent:<name>` wins; `agent:any` or `agent:<role>`
    is a pool. Either way the author of the thing under review is never among them."""
    names = label_names(issue)
    author = author_of(issue)
    for a in agents:
        if f"agent:{a['name'].lower()}" in names:
            if a["name"].lower() == author:
                return [], f"the named agent, {a['name']}, wrote it"
            return [a], ""
    # sorted: an issue with two agent labels (e.g. agent:any and agent:qa) must choose the same
    # one every run; set order depends on PYTHONHASHSEED. 'any' sorts before a role, so it wins a tie.
    token = next((n.split(":", 1)[1].strip() for n in sorted(names) if n.startswith("agent:")), None)
    if not token:
        return [], "no agent label"
    pool = list(agents) if token == ANY else [a for a in agents if role_matches(a, token)]
    if not pool:
        return [], f"no agent has the role {token!r}"
    pool = [a for a in pool if a["name"].lower() != author]
    if not pool:
        return [], f"the author ({author}) is the only agent who could take it"
    return pool, ""


def activity(board, agents):
    """Agent name -> its most recent `updated_at` on the board, from one page of the 100 most
    recently updated issues. An agent outside that window is treated as idle longest."""
    seen = {}
    for issue in board.call("GET", "/issues?state=all&sort=updated&direction=desc&per_page=100") or []:
        if "pull_request" in issue:
            continue
        names = label_names(issue)
        at = issue.get("updated_at") or ""
        for a in agents:
            if f"agent:{a['name'].lower()}" in names and at > seen.get(a["name"], ""):
                seen[a["name"]] = at
    return seen


def repo_key(issue):
    """The repo a task touches: its `repo:<name>` label, else a known repo named in the title or body.
    '' means unknown, and an unknown task only ever runs in lane 0 (#79)."""
    for name in sorted(label_names(issue)):
        if name.startswith("repo:") and name[len("repo:"):].strip():
            return name[len("repo:"):].strip()
    text = ((issue.get("title") or "") + "\n" + (issue.get("body") or "")).lower()
    return next((r for r in REPOS if r in text), "")


def inflight(board, agents):
    """(agents, repos, unknown) held by the open `status:working` issues: what the other lane is on.

    Read from the board, not from a state file, so a killed lane leaves nothing behind. `unknown`
    is True when a working task names no repo we recognise: then a second lane could collide with
    it, so it stands down (a `repo:<name>` label lets it run)."""
    names, repos, unknown = set(), set(), False
    for issue in board.working():
        agent = agent_of(issue, agents)
        if agent:
            names.add(agent["name"].lower())
        key = repo_key(issue)
        if key:
            repos.add(key)
        else:
            unknown = True
    return names, repos, unknown


def load_roster(path):
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("agents", []), data


def limits_of(path):
    """(ceilings, preamble) from the root-only limits file. No file: no ceilings, no preamble."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}, ""
    except ValueError as e:
        raise Failure(f"cannot read {path}: {e}") from None
    return data.get("timeout_sec", {}), data.get("preamble", "")


def ceiling(agent, caps):
    """The agent's run timeout, never above the root-only ceiling for that agent."""
    want = int(agent.get("timeout_sec", DEFAULT_TIMEOUT))
    cap = caps.get(agent["name"], caps.get("default"))
    return min(want, int(cap)) if cap is not None else want


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


def spend_ok(openrouter, key):
    """Fails closed: no cap, or a reply that cannot be read, means no run."""
    reply = http("GET", openrouter.rstrip("/") + "/key", key, timeout=20)
    k = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(k, dict) or not k:
        return False, "OpenRouter's /key reply was empty or not what was expected; refusing to run until it reports the key's cap."
    if k.get("limit") is None:
        return False, "The OpenRouter key has no monthly cap (limit is null); refusing to run. Put a cap on the key, then set `status:todo` again."
    if k.get("limit_remaining") is None and k.get("usage") is None:
        try:
            Decimal(str(k["limit"]))
        except ArithmeticError:
            pass            # a cap that is not a number is reported just below
        else:
            return False, "OpenRouter's /key reply has a cap but reports neither a remaining balance nor usage; refusing to run until it reports what has been spent."
    try:
        left = Decimal(str(k["limit_remaining"] if k.get("limit_remaining") is not None else Decimal(str(k["limit"])) - Decimal(str(k.get("usage", 0)))))
    except ArithmeticError:
        return False, "OpenRouter's /key reply has a cap or balance that is not a number; refusing to run."
    if left <= RESERVE:
        return False, f"OpenRouter cap reached: ${left:.2f} left of ${k['limit']} (reserve ${RESERVE}). Raise the cap or wait for the reset, then set `status:todo` again."
    return True, ""


def tail(text, n=20):
    return "\n".join((text or "").strip().splitlines()[-n:]) or "(no output)"


def recover_stale(board, agents, caps, log, now=None):
    """Block status:working issues whose run can't still be going. Only called holding the dispatch lock,
    so no run is live: a dispatcher killed mid-run (reboot, OOM, the unit's TimeoutStartSec) leaves its issue
    `working`, and todo() would never look at it again. The age limit is the same ceiling the run itself got, so a
    run cut off by the root-only cap is swept at that cap, not the roster's longer timeout."""
    now = now or dt.datetime.now(dt.timezone.utc)
    for issue in board.working():
        agent = agent_of(issue, agents)
        limit = (ceiling(agent, caps) if agent else DEFAULT_TIMEOUT) + GRACE
        age = (now - dt.datetime.fromisoformat(issue["updated_at"])).total_seconds()
        if age <= limit:
            continue
        n = issue["number"]
        board.set_status(issue, "blocked")
        board.comment(n, f"Blocked by the dispatcher: `status:working` for {int(age // 60)} min with no update, "
                         f"past the {limit // 60} min limit; the run was probably killed. Set `status:todo` to try again.")
        log(f"#{n} blocked: stale working")


def hold_low_priority(todo, a, orkey, log, now=None):
    """The pacing rule, when the root-only limits file turns it on.

    Under the line: every task is admitted. Over the line, or with the CPU limit on: only
    `priority:low` issues wait; ordinary work still runs. No switch or no readable figures
    means no change (the existing spend check and $1 reserve still fail closed first).
    """
    setting = pacing_settings(a.limits)
    if not setting.get("enabled"):
        return todo
    now = now or dt.datetime.now(dt.timezone.utc)
    reasons = []
    usage = key_usage(a.openrouter, orkey)
    try:
        cap, spend = float(usage["limit"]), float(usage["usage_monthly"])
        p = month_pace(now, cap, spend)
        if float(spend) > p["linear_to_date"] + p["linear_per_day"] * float(setting.get("slack_days", 1.0)):
            reasons.append(f"spend ${spend:.2f} is ahead of the ${p['linear_to_date']:.2f} pace line")
    except (KeyError, TypeError, ValueError):
        log("pacing: spend figures unreadable; holding low-priority work (fail closed)")
        reasons.append("spend figures unreadable")
    try:
        first = cpu_sample()
        time.sleep(float(setting.get("sample_seconds", 1.0)))
        d = cpu_delta(first, cpu_sample())
        limit = float(setting.get("steal_pct", 10.0))
        if d["steal_pct"] > limit:
            reasons.append(f"steal {d['steal_pct']:.1f}% is over {limit:.0f}% (Hostinger's limit is on)")
    except (Failure, OSError, KeyError, ValueError) as e:
        log(f"pacing: cannot read CPU ({e}); holding low-priority work (fail closed)")
        reasons.append("CPU figures unreadable")
    if not reasons:
        return todo
    kept = [t for t in todo if "priority:low" not in {l["name"] for l in t[0]["labels"]}]
    if len(kept) < len(todo):
        log(f"pacing: holding {len(todo) - len(kept)} low-priority task(s): {'; '.join(reasons)}")
    return kept


def priority_rank(issue):
    """Pick order: `priority:high` first, then unlabelled, then `priority:low`. Anything else ranks 1.

    Petter, 4 Nov: a bug must not wait behind older feature requests. With no priority labels every
    issue ranks 1, so the pick is oldest-first exactly as before.
    """
    names = label_names(issue)
    if "priority:high" in names:
        return 0
    return 2 if "priority:low" in names else 1


def choose_and_claim(a, board, agents, caps, orkey, log, lane=0):
    """Sweep, pick one issue and claim it. Runs under the pick lock (run_one), so two lanes never
    claim the same issue and never sweep at the same time; the lock is released before the run.

    Lane 0 takes the oldest admissible task, as always. Lane 1 takes the oldest task it may run
    *beside* the other lane: a different agent, and a repo no live run is in. The repo is the
    `repo:<name>` label, else a known repo named in the title or body; a task with neither, or a
    live task with neither, stands lane 1 down (two of them could touch the same repo) -- #79.
    Returns (issue, agent, pooled, timeout, queue_before, held) or None."""
    recover_stale(board, agents, caps, log)
    seen, todo, unassignable, offered = None, [], [], 0
    # Symmetric exclusion: with lanes = 2 even lane 0 keeps off the agent and repo the other lane is in.
    # With one lane nothing is read and nothing changes. Lane 0 never stands down for an unknown repo.
    two = bool(lane) or lanes_setting(a.limits) == 2
    busy_agents, busy_repos, unknown = inflight(board, agents) if two else (set(), set(), False)
    for issue in board.todo():
        pool, why = candidates(issue, agents)
        if not pool:
            if why != "no agent label":
                unassignable.append((issue, why))
            continue
        offered += 1
        if seen is None and len(pool) > 1:
            seen = activity(board, agents)      # one extra read, only when a task has a pool to choose from
        agent = min(pool, key=lambda who: (seen or {}).get(who["name"], ""))
        key = repo_key(issue)
        if lane and (agent["name"].lower() in busy_agents or unknown or not key or key in busy_repos):
            continue                            # the other lane's agent, or a repo it may be in
        if two and not lane and (agent["name"].lower() in busy_agents or (key and key in busy_repos)):
            continue                            # lane 0 beside a live lane-1 run
        pooled = not any(f"agent:{who['name'].lower()}" in label_names(issue) for who in agents)
        todo.append((issue, agent, pooled, key))
    if not todo:
        if unassignable:      # e.g. only its own author could take it: tell the Major instead of looping on it
            issue, why = unassignable[0]
            board.set_status(issue, "blocked")
            board.comment(issue["number"], f"Blocked by the dispatcher: {why}. Relabel it for an agent who may "
                                           f"take it, then set `status:todo` again.")
            log(f"#{issue['number']} blocked: {why}")
        elif lane:
            log(f"lanes: lane {lane} found nothing it may run beside lane 0")
        return None
    # Priority before age: a stable sort, so oldest-first survives within each priority group.
    todo.sort(key=lambda t: priority_rank(t[0]))
    ok, why = spend_ok(a.openrouter, orkey)
    if not ok:
        issue = todo[0][0]
        board.set_status(issue, "blocked")
        board.comment(issue["number"], why)
        log(f"#{issue['number']} blocked: spend")
        return None
    admitted = len(todo)                   # what this lane may take, before the pacing rule
    todo = hold_low_priority(todo, a, orkey, log)
    if not todo:
        log("pacing: nothing left to admit")
        return None
    issue, agent, pooled, _key = todo[0]
    board.set_status(issue, "working", agent if pooled else None)
    log(f"#{issue['number']} -> {agent['name']}" + (f" (lane {lane})" if lane else ""))
    return issue, agent, pooled, ceiling(agent, caps), offered, admitted - len(todo)


def run_one(a, board, hermes_key, orkey, log, lane=0):
    agents, _ = load_roster(a.roster)
    caps, preamble = limits_of(a.limits)
    for ag in agents:
        ag["_root"] = a.repo_root
    lock = open(Path(a.state_dir) / PICK_LOCK, "w")
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("lanes: the other lane is choosing; nothing this tick")
            return
        picked = choose_and_claim(a, board, agents, caps, orkey, log, lane)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()
    if picked is None:
        return
    issue, agent, pooled, timeout, queue_before, held = picked
    n = issue["number"]
    system, user = prompt_for(agent, issue, preamble, Path(a.secrets_dir) / "briefs")
    started = dt.datetime.now(dt.timezone.utc)
    usage_before, cpu_before = key_usage(a.openrouter, orkey), None
    try:
        cpu_before = cpu_sample()
    except (Failure, OSError):
        pass
    out, problem, reply = "", "", None
    try:
        reply = http("POST", a.hermes.rstrip("/") + "/v1/chat/completions", hermes_key, timeout=timeout,
                     headers={"X-Hermes-Session-Id": f"issue-{n}"},
                     body={"model": "hermes-agent", "messages": [{"role": "system", "content": system},
                                                                  {"role": "user", "content": user}]})
        out = (((reply or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Failure as e:
        problem = str(e)
    ended = dt.datetime.now(dt.timezone.utc)
    # Re-read: the agent reports with s9-task, which changes the labels.
    now = board.call("GET", f"/issues/{n}")
    labels = {l["name"] for l in now["labels"]}
    if problem or "status:working" in labels:
        outcome = ("timeout" if "timed out" in problem.lower() else "blocked") if problem else "no-output"
        board.set_status(now, "blocked")
        board.comment(n, f"Blocked by the dispatcher: {problem or 'the run ended without a final status'}.\n\nLast lines of output:\n```\n{tail(out)}\n```")
        log(f"#{n} blocked: {problem or 'no final status'}")
    else:
        outcome = "blocked" if "status:blocked" in labels else "ok"
        log(f"#{n} finished")
    usage_after = key_usage(a.openrouter, orkey)
    cpu, cpu_after = None, None
    try:
        if cpu_before is not None:
            cpu_after = cpu_sample()
            cpu = cpu_delta(cpu_before, cpu_after)
    except (Failure, OSError):
        cpu, cpu_after = None, None
    tokens = None
    if isinstance(reply, dict) and isinstance(reply.get("usage"), dict):
        tokens = {k: v for k, v in reply["usage"].items() if isinstance(v, (int, float))}
    try:                                    # building and writing the record: neither may stop a run
        record = {
            "v": LEDGER_VERSION, "issue": n, "type": task_type(issue.get("title")),
            "agent": agent["name"], "role": agent.get("role", ""),
            "model": agent.get("model", "hermes-agent"),
            "start": started.isoformat(timespec="seconds"), "end": ended.isoformat(timespec="seconds"),
            "seconds": round(max((ended - started).total_seconds(), 0.0), 3),
            "outcome": outcome, "lane": lane, "queue_before": queue_before,
            "held_low_priority": held,
            "usage_before": usage_before, "usage_after": usage_after,
            "cost_usd": money_delta(usage_before, usage_after),
            "tokens": tokens, "cpu": cpu, "cpu_before": cpu_before, "cpu_after": cpu_after,
        }
        append_ledger(Path(a.state_dir) / LEDGER_NAME, record)
    except Exception as e:                  # a ledger must never stop or fail a run
        log(f"ledger: not written for #{n} ({e})")


def field_matches(spec, value, lo, hi):
    for part in spec.split(","):
        rng, _, step = part.partition("/")
        first, _, last = rng.partition("-")
        start, end = (lo, hi) if rng == "*" else (int(first), int(last or first))
        if start <= value <= end and (value - start) % int(step or 1) == 0:
            return True
    return False


def cron_matches(cron, t):
    m, h, dom, mon, dow = cron.split()
    return (field_matches(m, t.minute, 0, 59) and field_matches(h, t.hour, 0, 23)
            and field_matches(dom, t.day, 1, 31) and field_matches(mon, t.month, 1, 12)
            and field_matches(dow, (t.weekday() + 1) % 7, 0, 6))   # 0 = Sunday


def last_fire(cron, tz, since, now):
    """Latest minute in (since, now] matching cron in tz, else None."""
    t = now.astimezone(tz).replace(second=0, microsecond=0)
    limit = since.astimezone(tz)
    for _ in range(3 * 24 * 60):          # catch up at most 3 days after downtime
        if t <= limit:
            return None
        if cron_matches(cron, t):
            return t
        t -= dt.timedelta(minutes=1)
    return None


def routines(a, board, log):
    agents, data = load_roster(a.roster)
    items = [(x["name"], x["routine"]) for x in agents if x.get("routine", {}).get("active")]
    priv = Path(a.private_routines)
    if priv.exists():       # root-only extras kept out of the public repo: [[routines]] agent/title/description/cron/timezone
        items += [(r["agent"], {**r, "active": True}) for r in tomllib.loads(priv.read_text()).get("routines", [])]
    now = dt.datetime.now(dt.timezone.utc)
    state_path = Path(a.state_dir) / "routines.json"
    try:
        since = dt.datetime.fromisoformat(json.loads(state_path.read_text())["at"])
    except (OSError, ValueError, KeyError):
        since = now           # first run: no backfill
    for name, r in items:
        when = last_fire(r["cron"], ZoneInfo(r.get("timezone", "UTC")), since, now)
        if not when:
            continue
        label = f"agent:{name.lower()}"
        open_ = board.pages(f"/issues?labels={urllib.parse.quote(label)}&state=open")
        if any(i["title"] == r["title"] for i in open_):
            log(f"{name}: '{r['title']}' is still open; skipped")
            continue
        board.call("POST", "/issues", {"title": r["title"], "body": r.get("description", ""), "labels": [label, "status:todo"]})
        log(f"{name}: opened '{r['title']}' ({when:%Y-%m-%d %H:%M %Z})")
    Path(a.state_dir).mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"at": now.isoformat()}))


def status(a, board, hermes_key, orkey, log):
    todo = board.todo()
    log(f"queue: {len(todo)} todo ({', '.join('#%d' % i['number'] for i in todo[:10]) or 'none'})")
    ok, why = spend_ok(a.openrouter, orkey)
    log("spend: ok" if ok else "spend: " + why)
    try:
        http("GET", a.hermes.rstrip("/") + "/v1/models", hermes_key, timeout=10)
        log("hermes: reachable")
    except Failure as e:     # any HTTP answer, even an error, means it is up
        log("hermes: " + ("NOT reachable (" + str(e) + ")" if "no response" in str(e) else "reachable"))


def spend(a, board, orkey, log):
    """Keep one open issue titled "Spend" current: OpenRouter's own month, cap, left and today."""
    k = (http("GET", a.openrouter.rstrip("/") + "/key", orkey, timeout=20) or {}).get("data") or {}
    if "usage_monthly" not in k:
        raise Failure("OpenRouter /key reply has no usage_monthly")
    money = lambda key: f"${Decimal(str(k[key])):.2f}" if k.get(key) is not None else "n/a"
    now = dt.datetime.now(dt.timezone.utc)
    body = "\n".join([
        f"**Live snapshot, updated hourly by the VPS.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        "OpenRouter (the key the agents use; true cost):",
        f"- This month (UTC): {money('usage_monthly')}",
        f"- Cap: {money('limit')}" + (f" ({k['limit_reset']} reset)" if k.get("limit_reset") else ""),
        f"- Left: {money('limit_remaining')}",
        f"- Today (UTC): {money('usage_daily')}", "",
        "Rewritten every hour; don't edit. Leave this issue open and pinned."])
    open_ = board.pages("/issues?state=open&sort=created&direction=asc")
    issue = next((i for i in open_ if i["title"] == "Spend" and "pull_request" not in i), None)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": body})
        log(f"spend: updated #{issue['number']}")
        return
    issue = board.call("POST", "/issues", {"title": "Spend", "body": body})
    log(f"spend: opened #{issue['number']}")
    try:        # pinning is GraphQL-only and needs more than issue access; best effort
        http("POST", f"{board.api}/graphql", board.token, {
            "query": "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
            "variables": {"id": issue["node_id"]}})
    except (Failure, KeyError) as e:
        log(f"spend: could not pin #{issue['number']} (pin it by hand): {e}")


def parse_when(value):
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def summarise(rows):
    """One bucket of ledger rows: runs, busy time, money, failures and CPU credits."""
    out = {"runs": len(rows), "seconds": 0.0, "cost": 0.0, "priced": 0,
           "timeouts": 0, "timeout_seconds": 0.0, "credits": 0.0,
           "outcomes": {"ok": 0, "timeout": 0, "blocked": 0, "no-output": 0}}
    for r in rows:
        seconds = float(r.get("seconds") or 0.0)
        out["seconds"] += seconds
        if isinstance(r.get("cost_usd"), (int, float)):
            out["cost"] += float(r["cost_usd"])
            out["priced"] += 1
        cpu = r.get("cpu") or {}
        out["credits"] += float(cpu.get("credits") or 0.0)
        status = r.get("outcome")
        if status in out["outcomes"]:
            out["outcomes"][status] += 1
        if status == "timeout":
            out["timeouts"] += 1
            out["timeout_seconds"] += seconds
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


def money(value):
    return f"${value:.2f}" if isinstance(value, (int, float)) else "—"


def rate(hours, cost, priced):
    """$/hour only when the sample is long enough to mean something (72 s)."""
    return f"${cost / hours:.2f}" if hours >= 0.02 and priced else "—"


def table(rows, label):
    lines = [f"| {label} | runs | busy h | $ | $/h | timeouts | timeout h | credits |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for name, group in rows:
        s = summarise(group)
        lines.append(f"| {name} | {s['runs']} | {s['hours']:.2f} | {money(s['cost']) if s['priced'] else '—'} | "
                     f"{rate(s['hours'], s['cost'], s['priced'])} | {s['timeouts']} | "
                     f"{s['timeout_seconds'] / 3600.0:.2f} | {s['credits']:.0f} |")
    return lines


def capacity(a, board, orkey, log):
    """Keep one open issue titled "Capacity" current: the ledger rolled up, and the pace line."""
    now = dt.datetime.now(dt.timezone.utc)
    usage = key_usage(a.openrouter, orkey)
    rows = read_ledger(Path(getattr(a, "ledger", "") or (Path(a.state_dir) / LEDGER_NAME)))
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = now - dt.timedelta(days=7)
    in_window = lambda start: [r for r in rows if parse_when(r.get("start")) and parse_when(r["start"]) >= start]  # noqa: E731
    week, mtd = in_window(week_start), in_window(month_start)
    ws, ms = summarise(week), summarise(mtd)
    lines = [f"**Capacity, rolled up by the VPS from the run ledger.** As of {now:%Y-%m-%d %H:%M} UTC.", ""]
    cap, spend = usage.get("limit"), usage.get("usage_monthly")
    if isinstance(cap, (int, float)) and isinstance(spend, (int, float)):
        p = month_pace(now, cap, spend)
        left = float(cap) - float(spend)
        ahead = float(spend) - p["linear_to_date"]
        today = usage.get("usage_daily")
        lines += [f"**Pace:** {money(spend)} of {money(cap)} used; day {p['elapsed']:.1f} of {p['days']}.",
                  f"- Fixed linear pace: {money(p['linear_per_day'])}/day, so {money(p['linear_to_date'])} "
                  f"by now. Actual is {'ahead by ' + money(ahead) if ahead >= 0 else 'behind by ' + money(-ahead)}"
                  f"{' (' + format(float(spend) / p['linear_to_date'], '.1f') + 'x)' if p['linear_to_date'] else ''}.",
                  f"- Remaining-days pace (after the ${RESERVE:.0f} reserve): {money(p['remaining_per_day'])}/day "
                  f"for {p['left_days']:.1f} days, from {money(left)} left.",
                  f"- Today (UTC): {money(today)} used, fixed pace {money(p['linear_per_day'])}.",
                  ""]
    else:
        lines += ["**Pace:** OpenRouter's cap or month-to-date usage could not be read; no pace line this run.", ""]
    lines += ["**Last 7 days**",
              f"- Runs: {ws['runs']}; busy: {ws['hours']:.2f} h; spend: {money(ws['cost']) if ws['priced'] else '—'}"
              f"{' (from ' + str(ws['priced']) + (' priced run)' if ws['priced'] == 1 else ' priced runs)') if ws['priced'] else ' (no run had a readable cost delta)'}.",
              f"- Timeouts: {ws['timeouts']} ({ws['timeout_seconds'] / 3600.0:.2f} h wasted); outcomes: "
              + ", ".join(f"{k} {v}" for k, v in ws["outcomes"].items()) + ".",
              f"- Queue idle (upper bound): {queue_idle_hours(week):.2f} h.",
              f"- CPU credits: {ws['credits']:.0f} against a {SAFE_CREDITS_DAY * 7:.0f} guidance for 7 days "
              f"({100.0 * ws['credits'] / (SAFE_CREDITS_DAY * 7):.0f}%).",
              f"- Lanes: {lane_line(week)}.", "",
              "**Month to date**",
              f"- Runs: {ms['runs']}; busy: {ms['hours']:.2f} h; spend: {money(ms['cost']) if ms['priced'] else '—'}.",
              f"- Timeouts: {ms['timeouts']} ({ms['timeout_seconds'] / 3600.0:.2f} h wasted); "
              f"queue idle (upper bound): {queue_idle_hours(mtd):.2f} h.",
              f"- CPU credits: {ms['credits']:.0f} against a {SAFE_CREDITS_DAY * max(p['elapsed'], 1):.0f} "
              f"guidance for {max(p['elapsed'], 1):.1f} days "
              f"({100.0 * ms['credits'] / (SAFE_CREDITS_DAY * max(p['elapsed'], 1)):.0f}%)."
              if isinstance(cap, (int, float)) and isinstance(spend, (int, float)) else
              f"- CPU credits: {ms['credits']:.0f}.", ""]
    by_agent = {}
    for r in week:
        by_agent.setdefault(r.get("agent") or "unknown", []).append(r)
    by_type = {}
    for r in week:
        by_type.setdefault(r.get("type") or "task", []).append(r)
    lines += ["**Last 7 days by agent**"] + table(sorted(by_agent.items()), "Agent") + [""]
    lines += ["**Last 7 days by task type**"] + table(sorted(by_type.items()), "Type") + [""]
    stale = [r for r in rows if not isinstance(r.get("cost_usd"), (int, float))]
    if stale:
        lines += [f"- {len(stale)} run(s) have no cost figure: OpenRouter's usage counter did not move or "
                  "could not be read. Nothing was guessed; collect the next run's `usage_before`/`usage_after`.", ""]
    if not rows:
        lines += [f"No ledger data yet. The first run appends to `{Path(a.state_dir) / LEDGER_NAME}`.", ""]
    lines += ["Rewritten by `vps/dispatch.py capacity`; don't edit. Leave this issue open and pinned."]
    body = "\n".join(lines)
    open_ = board.pages("/issues?state=open&sort=created&direction=asc")
    issue = next((i for i in open_ if i["title"] == "Capacity" and "pull_request" not in i), None)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": body})
        log(f"capacity: updated #{issue['number']}")
        return
    issue = board.call("POST", "/issues", {"title": "Capacity", "body": body})
    log(f"capacity: opened #{issue['number']}")
    try:        # pinning is GraphQL-only and needs more than issue access; best effort
        http("POST", f"{board.api}/graphql", board.token, {
            "query": "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
            "variables": {"id": issue["node_id"]}})
    except (Failure, KeyError) as e:
        log(f"capacity: could not pin #{issue['number']} (pin it by hand): {e}")


def cpu_fields():
    """The first ``cpu`` line of /proc/stat as ints: user nice system idle iowait irq softirq steal."""
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith("cpu "):
            return [int(v) for v in line.split()[1:]]
    raise Failure("/proc/stat has no cpu line")


def busy_seconds(fields):
    """Core-seconds this VPS actually used in a ``cpu`` line (idle, iowait and steal are not ours).

    /proc/stat counts USER_HZ ticks (normally 100 a second), not seconds. Treating them as
    seconds made the hourly draw read ~100x too high (section9-hq #44: 117,609 credits/hour).
    """
    return float(sum(fields[:3]) + sum(fields[5:7])) / CLK_TCK


def cpu_sample():
    """A cumulative CPU snapshot, in seconds, for the run ledger."""
    fields = cpu_fields()
    return {"at": time.time(), "busy": busy_seconds(fields),
            "steal": fields[7] / CLK_TCK, "total": sum(fields) / CLK_TCK}


def cpu_delta(before, after):
    """What one run cost in CPU: steal share, core-seconds and credits above the baseline.

    Credits are core-seconds above Hostinger's free 25% baseline (section9-hq #34). This is
    an estimate from /proc/stat: it includes everything on the VPS, not only the run, so a
    single lane makes it attributable; two lanes do not.
    """
    wall = max(after["at"] - before["at"], 0.0)
    total = max(after["total"] - before["total"], 0.0)
    busy = max(after["busy"] - before["busy"], 0.0)
    steal = max(after["steal"] - before["steal"], 0.0)
    return {"wall_seconds": round(wall, 3),
            "steal_pct": round(100.0 * steal / total, 2) if total else 0.0,
            "core_seconds": round(busy, 3),
            "credits": round(max(0.0, busy - BASELINE * wall), 3)}


def key_usage(openrouter, key):
    """OpenRouter's counters, best effort: a ledger must never stop a run or a report.

    The 10 s timeout bounds a slow read. One run reads it twice, before it starts and after it
    ends, so a slow OpenRouter can delay a run's start by up to 10 s (docs/CAPACITY.md).
    """
    try:
        reply = http("GET", openrouter.rstrip("/") + "/key", key, timeout=10)
    except Exception:
        return {}
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        return {}
    return {name: data.get(name) for name in
            ("usage", "usage_monthly", "usage_daily", "limit", "limit_remaining")}


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


def task_type(title):
    """A coarse task type from the title's prefix ('QA: ...', 'Research: ...'), for grouping only."""
    m = re.match(r"\s*([A-Za-z]+)\s*:", title or "")
    word = m.group(1).lower() if m else ""
    return word if word in TYPE_PREFIXES else "task"


def append_ledger(path, record):
    """Append one JSON line, mode 0640 in a 0750 dir. Raises; the caller decides that a ledger
    failure must not matter. The record holds every run's cost and CPU draw: not world-readable."""
    p = Path(path)
    p.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o640)
    with os.fdopen(fd, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def read_ledger(path):
    """Every good record; a corrupt line is skipped rather than losing the whole report."""
    rows = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return rows
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("start"):
            rows.append(row)
    return rows


def month_pace(now, cap, spend, reserve=RESERVE):
    """The two pace lines: fixed linear for the month, and what is left over the days left."""
    days = calendar.monthrange(now.year, now.month)[1]
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elapsed = max((now - start).total_seconds() / 86400.0, 0.0)
    linear = max(float(cap) - float(reserve), 0.0) / days
    left_days = max(days - elapsed, 0.0)
    remaining = float(cap) - float(spend) - float(reserve)
    return {"days": days, "elapsed": elapsed, "left_days": left_days,
            "linear_per_day": linear, "linear_to_date": linear * elapsed,
            "remaining_per_day": (remaining / left_days) if left_days > 0 else 0.0}


def pacing_settings(path):
    """The root-only [pacing] table. Absent file or key: pacing is off (no behaviour change)."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    value = data.get("pacing", {})
    return value if isinstance(value, dict) else {}


def over_pace(now, cap, spend, reserve=RESERVE, slack_days=1.0):
    """True when month-to-date spend is more than `slack_days` ahead of the linear pace."""
    p = month_pace(now, cap, spend, reserve)
    return float(spend) > p["linear_to_date"] + p["linear_per_day"] * slack_days


def lanes_setting(path):
    """The root-only `lanes = 2` switch. Absent, unreadable, or anything but 2: one lane (no change)."""
    try:
        value = tomllib.loads(Path(path).read_text(encoding="utf-8")).get("lanes", 1)
    except (OSError, ValueError):
        return 1
    return 2 if value == 2 else 1


def steal_secs(seconds):
    """CPU steal over `seconds` of wall clock. Two /proc/stat reads: it spends no CPU of its own."""
    first = cpu_sample()
    time.sleep(max(float(seconds), 0.0))
    return cpu_delta(first, cpu_sample())["steal_pct"]


def ledger_credits(rows, now, hours=24.0):
    """CPU credits above the baseline that the runs of the last `hours` drew, from the ledger.

    Per-run figures, so with two lanes each counts some of the other's draw: the sum over-estimates,
    which is the safe direction for a fallback."""
    since = now - dt.timedelta(hours=hours)
    return sum(float((r.get("cpu") or {}).get("credits") or 0.0)
               for r in rows if parse_when(r.get("start")) and parse_when(r["start"]) >= since)


def lane_fallback(a, rows, now):
    """Why a second lane must not run right now, or ''. Steal first (Hostinger's temporary limit
    shows there, as the hourly CPU credits issue reads it), then the runs' own credit draw."""
    setting = pacing_settings(a.limits)
    try:
        steal = steal_secs(setting.get("sample_seconds", 1.0))
    except (Failure, OSError, KeyError, ValueError) as e:
        return f"cpu unreadable ({e})"
    limit = float(setting.get("steal_pct", SAFE_STEAL_PCT))
    if steal > limit:
        return f"CPU steal {steal:.1f}% is over {limit:.0f}%: Hostinger's limit is on"
    draw = ledger_credits(rows, now)
    if draw >= SAFE_CREDITS_DAY:
        return f"the last 24 h drew {draw:.0f} CPU credits, over the {SAFE_CREDITS_DAY:.0f}/day guidance"
    return ""


def write_lane_state(path, state):
    """The latch is advice for the next tick: a failed write is logged, never fatal."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as e:
        print(f"lanes: cannot write {path}: {e}", flush=True)


def lane_state(a, log, now=None):
    """(lanes allowed, why). The root-only switch, the fallback latch and the quiet period.

    Two lanes only while the switch says 2, the reading is safe, and `quiet_hours` have passed
    since the last unsafe one. An unsafe reading drops to one lane and writes the latch; the lane
    returns only after that quiet period, so a single steal spike cannot flap it (#79)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    if lanes_setting(a.limits) < 2:
        return 1, "the lanes switch is 1"
    path = Path(a.state_dir) / LANE_STATE
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    rows = read_ledger(Path(getattr(a, "ledger", "") or (Path(a.state_dir) / LEDGER_NAME)))
    reason = lane_fallback(a, rows, now)
    if reason:
        write_lane_state(path, {"lanes": 1, "at": now.isoformat(timespec="seconds"),
                                "unsafe_at": now.isoformat(timespec="seconds"), "reason": reason})
        log(f"lanes: down to one lane: {reason}")
        return 1, reason
    unsafe_at = parse_when(state.get("unsafe_at"))
    quiet = float(pacing_settings(a.limits).get("quiet_hours", DEFAULT_QUIET_HOURS))
    if unsafe_at and (now - unsafe_at).total_seconds() < quiet * 3600.0:
        left = (quiet * 3600.0 - (now - unsafe_at).total_seconds()) / 60.0
        return 1, f"quiet period: {left:.0f} min left of {quiet:.0f} h after an unsafe reading"
    if state.get("lanes") != 2:
        write_lane_state(path, {"lanes": 2, "at": now.isoformat(timespec="seconds"),
                                "unsafe_at": state.get("unsafe_at"), "reason": ""})
        log("lanes: back to two lanes")
    return 2, ""


def concurrent_hours(rows):
    """Hours with two or more runs in flight at once: how much the second lane was actually used."""
    events = []
    for r in rows:
        start, end = parse_when(r.get("start")), parse_when(r.get("end"))
        if start and end and end > start:
            events += [(start, 1), (end, -1)]
    events.sort()                       # at equal times the end (-1) sorts first: no zero-length overlap
    live, hours, last = 0, 0.0, None
    for at, delta in events:
        if last is not None and live >= 2:
            hours += (at - last).total_seconds() / 3600.0
        live += delta
        last = at
    return hours


def lane_line(rows):
    """Runs and busy hours per lane, and the hours two ran at once. A row without a `lane` is one of
    the single-lane runs from before the switch existed: lane 0."""
    by_lane = {}
    for r in rows:
        by_lane.setdefault(int(r.get("lane") or 0), []).append(r)
    if not by_lane:
        return "—"
    parts = [f"lane {n}: {len(g)} run(s), {summarise(g)['hours']:.2f} h" for n, g in sorted(by_lane.items())]
    return "; ".join(parts) + f"; two at once {concurrent_hours(rows):.2f} h"


def cpu_ceiling():
    """CPU one thread gets per second of wall clock: ~1.0 uncapped, ~0.25 while Hostinger's
    limit is on. One second of load, so the snapshot costs ~18 credits a day at worst."""
    started = time.process_time()
    until = time.monotonic() + PROBE_SECONDS
    while time.monotonic() < until:
        pass
    return time.process_time() - started


def credits(a, board, log):
    """Keep one open issue titled "CPU credits" current: steal, the measured ceiling and the draw
    on Hostinger's balance. Read from this VPS only; no Hostinger API key is used or needed."""
    first = cpu_fields()
    time.sleep(SAMPLE_SECONDS)
    second = cpu_fields()
    total = float(sum(y - x for x, y in zip(first, second))) or 1.0
    share = lambda i: 100.0 * (second[i] - first[i]) / total if i < len(second) else 0.0  # noqa: E731
    ceiling = cpu_ceiling()
    now = dt.datetime.now(dt.timezone.utc)
    Path(a.state_dir).mkdir(parents=True, exist_ok=True)
    state = Path(a.state_dir) / "cpu-credit.json"
    try:
        before = json.loads(state.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        before = {}
    if before.get("v") != 2:          # the first, tick-as-seconds snapshot: its busy value is apples to oranges
        before = {}
    draw = None
    if before.get("at") and before.get("busy") is not None:
        hours = max((now.timestamp() - float(before["at"])) / 3600.0, 0.01)
        draw = (busy_seconds(second) - float(before["busy"]) - BASELINE * hours * 3600.0) / hours
    state.write_text(json.dumps({"v": 2, "at": now.timestamp(), "busy": busy_seconds(second)}), encoding="utf-8")
    try:
        some = next(l for l in Path("/proc/pressure/cpu").read_text(encoding="utf-8").splitlines()
                    if l.startswith("some"))
        psi = next((f for f in some.split() if f.startswith("avg60=")), "n/a")
    except (OSError, StopIteration):
        psi = "n/a"
    lines = [
        f"**Live snapshot, updated hourly by the VPS.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        # A short ceiling alone can be our own agents' load; Hostinger's limit shows as steal too.
        f"- **Limit: {'ON' if ceiling < 0.9 and share(7) > 10 else 'off'}** — one thread gets {ceiling:.2f} of a core.",
        f"- **Steal: {share(7):.1f}%** of the last {SAMPLE_SECONDS:.0f} s"
        " (Hostinger took that time; the temporary limit is what does it).",
        f"- Busy: {100.0 - share(3) - share(4) - share(7):.1f}% of one vCPU, iowait {share(4):.1f}%, PSI cpu {psi}.",
    ]
    if draw is not None:
        lines.append(f"- **Draw: ~{draw:.0f} credits/hour** ({100.0 * draw / BALANCE_MAX:.0f}% of the "
                     f"{BALANCE_MAX:.0f}-credit balance an hour) since the previous snapshot.")
    rate = 3600.0 * (SANDBOX_CORES + HERMES_CORES - BASELINE)
    lines += ["",
              f"Caps in force: sandbox {SANDBOX_CORES} + hermes {HERMES_CORES} = "
              f"{SANDBOX_CORES + HERMES_CORES:.2f} of one vCPU = {rate:.0f} credits/hour, so a full "
              f"balance is {BALANCE_MAX / rate:.1f} h at the cap.",
              "", "Rewritten every hour; don't edit. Leave this issue open."]
    body = "\n".join(lines)
    open_ = board.pages("/issues?state=open&sort=created&direction=asc")
    issue = next((i for i in open_ if i["title"] == "CPU credits" and "pull_request" not in i), None)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": body})
        log(f"cpu: updated #{issue['number']}")
        return
    issue = board.call("POST", "/issues", {"title": "CPU credits", "body": body})
    log(f"cpu: opened #{issue['number']}")


def note(a, board, log):
    """Keep one open issue titled "Sync" current: what the VPS did with itself. Rewrites the body."""
    if a.state not in ("ok", "blocked"):
        raise Failure("note needs --state ok or --state blocked")
    now = dt.datetime.now(dt.timezone.utc)
    body = "\n".join([
        f"**VPS sync: {a.state}.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        a.message, "",
        "Rewritten by `vps/sync.sh` on the VPS; don't edit. Leave this issue open."])
    labels = ["status:blocked"] if a.state == "blocked" else []
    open_ = board.call("GET", "/issues?state=open&sort=created&direction=asc&per_page=100") or []
    issue = next((i for i in open_ if i["title"] == "Sync" and "pull_request" not in i), None)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": body, "labels": labels})
        log(f"sync: #{issue['number']} {a.state}")
        return
    issue = board.call("POST", "/issues", {"title": "Sync", "body": body, "labels": labels})
    log(f"sync: opened #{issue['number']} ({a.state})")


def local_only(url, what):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" and u.hostname not in LOOPBACK:
        raise Failure(f"refusing to send {what} over plain HTTP to {u.hostname}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("cmd", nargs="?", default="run",
                   choices=["run", "routines", "status", "spend", "cpu", "note", "capacity"])
    p.add_argument("--secrets-dir", default="/etc/section9")
    p.add_argument("--state-dir", default="/var/lib/section9")
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--roster", default=str(ROOT / "agents" / "roster.toml"))
    p.add_argument("--private-routines", default="/etc/section9/routines.private.toml")
    p.add_argument("--limits", default="/etc/section9/limits.toml",
                   help="root-only run ceilings and the guardrail preamble")
    p.add_argument("--ledger", default="", help="run ledger path; default <state-dir>/runs.jsonl")
    p.add_argument("--lane", type=int, default=0, choices=(0, 1),
                   help="dispatch lane; 1 is the optional second lane (#79)")
    p.add_argument("--state", default="", help="note: ok or blocked")
    p.add_argument("--message", default="", help="note: what the VPS did")
    p.add_argument("--github", default="https://api.github.com")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    p.add_argument("--hermes", default="http://127.0.0.1:8642")
    a = p.parse_args(argv)

    def log(msg):
        print(redact(msg), flush=True)

    try:
        sd = Path(a.secrets_dir)
        ghtoken = read_env_value(sd / "sandbox.env", "GH_TOKEN")
        orkey = read_env_value(sd / "hermes.env", "OPENROUTER_API_KEY")
        hkey = read_env_value(sd / "hermes.env", "API_SERVER_KEY")
        secrets.extend([ghtoken, orkey, hkey])
        for url, what in ((a.github, "the GitHub token"), (a.hermes, "the Hermes key")):
            local_only(url, what)
        board = Board(a.github, ghtoken)
        if a.cmd == "routines":
            routines(a, board, log)
            return 0
        if a.cmd == "status":
            status(a, board, hkey, orkey, log)
            return 0
        if a.cmd == "spend":
            spend(a, board, orkey, log)
            return 0
        if a.cmd == "cpu":
            credits(a, board, log)
            return 0
        if a.cmd == "capacity":
            capacity(a, board, orkey, log)
            return 0
        if a.cmd == "note":
            note(a, board, log)
            return 0
        Path(a.state_dir).mkdir(parents=True, exist_ok=True)
        lanes = lanes_setting(a.limits)
        if a.lane >= lanes:
            log(f"lanes: lane {a.lane} is off (the root-only limits file says {lanes} lane(s))")
            return 0
        with open(Path(a.state_dir) / f"dispatch.lane{a.lane}.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log(f"another run is in progress in lane {a.lane}")
                return 0
            if a.lane:
                allowed, why = lane_state(a, log)     # the second lane decides for itself, and stands down
                if allowed < 2:
                    log(f"lanes: lane {a.lane} stands down: {why}")
                    return 0
            run_one(a, board, hkey, orkey, log, a.lane)
        return 0
    except Failure as e:
        print(f"dispatch: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
