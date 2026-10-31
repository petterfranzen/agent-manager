#!/usr/bin/env python3
"""Section 9 dispatcher: GitHub issues in section9-hq in, Hermes runs out.

  dispatch.py            run the oldest `agent:<name>` + `status:todo` issue
  dispatch.py routines   open the issues for routines whose cron time has come
  dispatch.py status     queue, spend and Hermes reachability (for the runbook)

Run by systemd timers on the VPS (vps/systemd). One run at a time (lock file).
Standard library only. Secrets come from root-only files and are never printed:
  OPENROUTER_API_KEY, API_SERVER_KEY  in <secrets-dir>/hermes.env
  GH_TOKEN                            in <secrets-dir>/sandbox.env
"""

import argparse
import datetime as dt
import fcntl
import json
import re
import sys
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


def http(method, url, token, body=None, timeout=30, headers=None):
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
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise Failure(f"{method} {redact(url)} -> {e.code}: {redact(e.read().decode('utf-8', 'replace'))[:300]}") from None
    except TimeoutError:
        raise Failure(f"{method} {redact(url)} -> timed out after {timeout} s") from None
    except (urllib.error.URLError, OSError) as e:
        raise Failure(f"{method} {redact(url)} -> no response: {redact(str(getattr(e, 'reason', e)))}") from None
    return json.loads(raw) if raw.strip() else None


class Board:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/"), token

    def call(self, method, path, body=None):
        return http(method, f"{self.api}/repos/{REPO}{path}", self.token, body)

    def todo(self):
        """Open status:todo issues (not PRs), oldest first."""
        issues = self.call("GET", "/issues?labels=status%3Atodo&state=open&sort=created&direction=asc&per_page=50")
        return [i for i in issues or [] if "pull_request" not in i]

    def comment(self, n, text):
        self.call("POST", f"/issues/{n}/comments", {"body": redact(text)[:60000]})

    def set_status(self, issue, status):
        labels = [l["name"] for l in issue["labels"] if not l["name"].startswith("status:")]
        self.call("PATCH", f"/issues/{issue['number']}", {"labels": labels + ["status:" + status]})


def agent_of(issue, agents):
    names = {l["name"].lower() for l in issue["labels"]}
    return next((a for a in agents if f"agent:{a['name'].lower()}" in names), None)


def load_roster(path):
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("agents", []), data


def prompt_for(agent, issue):
    """(system, user): the agent's brief, then the task."""
    brief = (Path(agent["_root"]) / agent["brief"]).read_text(encoding="utf-8")
    name, n = agent["name"], issue["number"]
    return brief, (
        f"Task #{n}: {issue['title']}\n{issue['html_url']}\n\n{issue.get('body') or '(no description)'}\n\n"
        f"You are {name}. Before you finish, report with "
        f"`s9-task --as {name} status {n} <review|done|blocked> \"message\"`."
    )


def spend_ok(openrouter, key):
    k = (http("GET", openrouter.rstrip("/") + "/key", key, timeout=20) or {}).get("data") or {}
    if k.get("limit") is None:
        return True, ""
    left = Decimal(str(k["limit_remaining"] if k.get("limit_remaining") is not None else Decimal(str(k["limit"])) - Decimal(str(k.get("usage", 0)))))
    if left <= RESERVE:
        return False, f"OpenRouter cap reached: ${left:.2f} left of ${k['limit']} (reserve ${RESERVE}). Raise the cap or wait for the reset, then set `status:todo` again."
    return True, ""


def tail(text, n=20):
    return "\n".join((text or "").strip().splitlines()[-n:]) or "(no output)"


def run_one(a, board, hermes_key, orkey, log):
    agents, _ = load_roster(a.roster)
    for ag in agents:
        ag["_root"] = a.repo_root
    todo = [(i, agent_of(i, agents)) for i in board.todo()]
    todo = [(i, ag) for i, ag in todo if ag]
    if not todo:
        return
    issue, agent = todo[0]
    n = issue["number"]
    ok, why = spend_ok(a.openrouter, orkey)
    if not ok:
        board.set_status(issue, "blocked")
        board.comment(n, why)
        log(f"#{n} blocked: spend")
        return
    system, user = prompt_for(agent, issue)
    board.set_status(issue, "working")
    log(f"#{n} -> {agent['name']}")
    timeout = int(agent.get("timeout_sec", 3600))
    out, problem = "", ""
    try:
        reply = http("POST", a.hermes.rstrip("/") + "/v1/chat/completions", hermes_key, timeout=timeout,
                     headers={"X-Hermes-Session-Id": f"issue-{n}"},
                     body={"model": "hermes-agent", "messages": [{"role": "system", "content": system},
                                                                  {"role": "user", "content": user}]})
        out = (((reply or {}).get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    except Failure as e:
        problem = str(e)
    # Re-read: the agent reports with s9-task, which changes the labels.
    now = board.call("GET", f"/issues/{n}")
    labels = {l["name"] for l in now["labels"]}
    if problem or "status:working" in labels:
        board.set_status(now, "blocked")
        board.comment(n, f"Blocked by the dispatcher: {problem or 'the run ended without a final status'}.\n\nLast lines of output:\n```\n{tail(out)}\n```")
        log(f"#{n} blocked: {problem or 'no final status'}")
    else:
        log(f"#{n} finished")


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
        open_ = board.call("GET", f"/issues?labels={urllib.parse.quote(label)}&state=open&per_page=100") or []
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


def local_only(url, what):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" and u.hostname not in LOOPBACK:
        raise Failure(f"refusing to send {what} over plain HTTP to {u.hostname}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("cmd", nargs="?", default="run", choices=["run", "routines", "status"])
    p.add_argument("--secrets-dir", default="/etc/section9")
    p.add_argument("--state-dir", default="/var/lib/section9")
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--roster", default=str(ROOT / "agents" / "roster.toml"))
    p.add_argument("--private-routines", default="/etc/section9/routines.private.toml")
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
        Path(a.state_dir).mkdir(parents=True, exist_ok=True)
        with open(Path(a.state_dir) / "dispatch.lock", "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                log("another run is in progress")
                return 0
            run_one(a, board, hkey, orkey, log)
        return 0
    except Failure as e:
        print(f"dispatch: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
