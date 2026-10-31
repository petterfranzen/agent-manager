#!/usr/bin/env python3
"""Unstick Paperclip's budgets after a run that never got its usage receipt.

When a run's process is lost (a restart, an OOM kill) or the run is cancelled,
Hermes never sends the usage receipt, so Paperclip keeps the run's accounting
"pending" for ever (heartbeat_runs.cost_accounting_pending, with
usage_json.accountingReceiptReady = false). A pending run counts as a hard
stop for its agent's and the company's budgets: Paperclip pauses them, and
refuses to resume ("Completed runs must finish accounting before resuming").
It accepts no cost event in place of the receipt, and has no API to close it.

vm-sync runs this on every tick (every 5 minutes). It:
  1. closes the accounting of runs that ended as process_lost or cancelled more
     than an hour ago and are still waiting for a receipt, and releases their
     held budget reservations (one guarded UPDATE in Paperclip's database);
  2. acknowledges the cancellation of cancelled runs that never got their stop
     proof, when their agent has been stuck "pausing" for over an hour, and
     asks Paperclip to resume that agent (see STOP_SQL);
  3. resolves each open hard-stop budget incident with "raise_budget_and_resume"
     at the same amount, so nothing is raised. Paperclip itself refuses this
     when booked spend has reached the budget, so a real overspend stays paused.

Safe because the money is not lost: spend-report.py books OpenRouter's real
spend every hour, whatever happened to the run's receipt. Runs as root.
Standard library only. The board key comes from <secrets-dir>/paperclip-board.env
and is never printed.
"""

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COMPANY = "Section 9"
KEYS = re.compile(r"(pcp_board_|sk-or-|github_pat_)[A-Za-z0-9_-]*")
# The runs it may close: ended, no receipt, lost or cancelled, quiet for an hour.
CLOSE_SQL = """
with closed as (
  update heartbeat_runs
     set cost_accounting_pending = false,
         usage_json = coalesce(usage_json, '{}'::jsonb) || '{"accountingClosedBy": "agent-manager-sync"}'::jsonb
   where cost_accounting_pending
     and usage_json->>'accountingReceiptReady' = 'false'
     and (status = 'cancelled' or (status = 'failed' and error_code = 'process_lost'))
     and coalesce(finished_at, created_at) < now() - interval '1 hour'
  returning id, agent_id, status
), released as (
  update budget_reservations set state = 'released', settled_at = now()
   where state = 'held' and run_id in (select id from closed)
  returning run_id
)
select coalesce(json_agg(json_build_object('run', c.id, 'agent', c.agent_id, 'status', c.status)), '[]')
  from closed c;
"""

# A cancelled legacy (Hermes gateway) run that never reached its process and
# never had its cancellation acknowledged has no "stop proof", so Paperclip's
# lifecycle driver keeps its agent "pausing" for ever
# (agent-execution-stop.ts, agentExecutionsHaveStopped). There is no API for
# it. Only for agents stuck pausing for an hour, on runs that ended an hour ago,
# with no process and no live controller lease.
STOP_SQL = """
with acked as (
  update heartbeat_runs r
     set result_json = coalesce(r.result_json, '{}'::jsonb)
           || '{"executionCancellation": {"state": "acknowledged", "by": "agent-manager-sync"}}'::jsonb
    from agents a
   where a.id = r.agent_id
     and a.lifecycle_state = 'pausing'
     and a.updated_at < now() - interval '1 hour'
     and r.status = 'cancelled'
     and r.runtime_mode = 'legacy'
     and r.process_pid is null and r.process_group_id is null
     and coalesce(r.execution_stage, '') <> 'settled'
     and coalesce(r.result_json->'executionCancellation'->>'state', '') <> 'acknowledged'
     and coalesce(r.finished_at, r.created_at) < now() - interval '1 hour'
     and (r.controller_lease_expires_at is null or r.controller_lease_expires_at < now())
  returning r.id, r.agent_id
)
select coalesce(json_agg(json_build_object('run', id, 'agent', agent_id)), '[]') from acked;
"""


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    for line in Path(path).read_text().splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == name:
            return v.strip().strip("'\"")
    raise Failure(f"{name} missing from {path}")


def psql(repo_dir, sql):
    """Runs SQL in the postgres container as Paperclip's user; returns stdout."""
    cmd = ["docker", "compose", "exec", "-T", "postgres",
           "psql", "-U", "paperclip", "-d", "paperclip", "-v", "ON_ERROR_STOP=1", "-tAq", "-c", sql]
    res = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, timeout=60)
    if res.returncode != 0:
        raise Failure("psql: " + res.stderr.strip().splitlines()[-1] if res.stderr.strip() else "psql failed")
    return res.stdout.strip()


class Api:
    def __init__(self, base, key):
        self.base = base.rstrip("/")
        self.key = key

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.key}", "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise Failure(f"{method} {path}: HTTP {e.code} {e.read()[:300].decode(errors='replace')}")
        except (urllib.error.URLError, TimeoutError) as e:
            raise Failure(f"{method} {path}: {e}")
        return json.loads(raw) if raw else None


def close_lost_runs(run_sql):
    out = run_sql(CLOSE_SQL)
    return json.loads(out or "[]")


def unstick_pausing(run_sql, api):
    """Acknowledges stop proof for stuck agents' cancelled runs, then resumes them."""
    acked = json.loads(run_sql(STOP_SQL) or "[]")
    resumed = []
    for agent in sorted({a["agent"] for a in acked}):
        try:
            api.call("POST", f"/api/agents/{urllib.parse.quote(agent, safe='')}/resume", {})
            resumed.append(agent)
        except Failure as e:
            # The lifecycle driver may need a moment; the Major or the next deploy resumes it.
            print(f"resume agent {agent}: {e}")
    return acked, resumed


def resume_incidents(api):
    """Resolves open hard-stop incidents at their current amount. Returns (resumed, refused)."""
    companies = api.call("GET", "/api/companies") or []
    company = next((c for c in companies if c.get("name") == COMPANY), None)
    if company is None:
        raise Failure(f"no company named {COMPANY}")
    cid = urllib.parse.quote(company["id"], safe="")
    overview = api.call("GET", f"/api/companies/{cid}/budgets/overview") or {}
    resumed, refused = [], []
    for inc in overview.get("activeIncidents") or []:
        if inc.get("status") != "open" or inc.get("thresholdType") != "hard":
            continue
        name = f"{inc.get('scopeType')} {inc.get('scopeName') or inc.get('scopeId')}"
        limit = inc.get("amountLimit")
        if not isinstance(limit, int) or limit <= 0:
            continue
        try:
            api.call("POST", f"/api/companies/{cid}/budget-incidents/{urllib.parse.quote(inc['id'], safe='')}/resolve",
                     {"action": "raise_budget_and_resume", "amount": limit,
                      "decisionNote": "agent-manager-sync: accounting closed; booked spend is under the budget"})
            resumed.append(name)
        except Failure as e:
            # 422 when booked spend is at the budget, or a run is still pending: leave it paused.
            refused.append(f"{name}: {e}")
    return resumed, refused


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo-dir", default="/opt/agent-manager")
    p.add_argument("--secrets-dir", default="/etc/agent-manager")
    p.add_argument("--paperclip-url", default="http://127.0.0.1:3100")
    a = p.parse_args(argv)
    try:
        key = read_env_value(Path(a.secrets_dir) / "paperclip-board.env", "PAPERCLIP_BOARD_KEY")
        secrets.append(key)
        closed = close_lost_runs(lambda sql: psql(a.repo_dir, sql))
        for c in closed:
            print(f"closed accounting of {c['status']} run {c['run']} (no receipt)")
        api = Api(a.paperclip_url, key)
        try:
            acked, unstuck = unstick_pausing(lambda sql: psql(a.repo_dir, sql), api)
        except Failure as e:  # never block the budget step below
            print(f"heal-accounting: unstick: {redact(str(e))}", file=sys.stderr)
            acked, unstuck = [], []
        for r in acked:
            print(f"acknowledged cancellation of run {r['run']} (agent stuck pausing)")
        for r in unstuck:
            print(f"asked to resume agent {r}")
        resumed, refused = resume_incidents(api)
        for r in resumed:
            print(f"resumed budget: {r}")
        for r in refused:
            print(f"left paused: {r}")
    except Failure as e:
        print(f"heal-accounting: {redact(str(e))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
