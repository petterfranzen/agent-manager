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
  CS_ACCESS_TOKEN                     in <secrets-dir>/codescene.env (optional; compose hands it
                                      to the sandbox, here it is only redacted from what we print)
Optional, root-only, outside the repo (a commit to it cannot change them):
  <secrets-dir>/limits.toml           per-agent run ceiling and the guardrail preamble
  <secrets-dir>/briefs/<Name>.md      an agent's brief, when it must not live in the repo

This file is the entry point only; the parts are in vps/dispatcher/ (its __init__.py lists them).
The constants other files ask to keep in step: SANDBOX_CORES/HERMES_CORES in dispatcher/cpu.py,
DEFAULT_MODEL in dispatcher/limits.py, PRICES in dispatcher/ledger.py.
"""

import argparse
import sys
from pathlib import Path

# The timers run this file with `python3 -I`, which leaves its folder off sys.path. The parts live
# beside it in vps/dispatcher/; appended, so nothing here can shadow the standard library.
sys.path.append(str(Path(__file__).resolve().parent))

from dispatcher import board, capacity, cpu, net, reports, routines, run   # noqa: E402
from dispatcher.context import Context                                     # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
# Every command but the default, `run` (one task in this lane).
COMMANDS = {"routines": routines.routines, "status": reports.status, "spend": reports.spend,
            "cpu": cpu.credits, "capacity": capacity.capacity, "note": reports.note}


def parser():
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
    p.add_argument("--health-file", default="", help="note: timer health lines to append")
    p.add_argument("--github", default="https://api.github.com")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    p.add_argument("--hermes", default="http://127.0.0.1:8642")
    return p


def log(msg):
    print(net.redact(msg), flush=True)


def optional_secret(path, name):
    """A secret the run can do without: "" when the file or the value is missing."""
    try:
        return net.read_env_value(path, name)
    except net.Failure:
        return ""


def context(a):
    """Read the secrets (never printed), refuse plain HTTP to a remote host, and open the board."""
    sd = Path(a.secrets_dir)
    ghtoken = net.read_env_value(sd / "sandbox.env", "GH_TOKEN")
    orkey = net.read_env_value(sd / "hermes.env", "OPENROUTER_API_KEY")
    hkey = net.read_env_value(sd / "hermes.env", "API_SERVER_KEY")
    cstoken = optional_secret(sd / "codescene.env", "CS_ACCESS_TOKEN")
    net.secrets.extend(s for s in (ghtoken, orkey, hkey, cstoken) if s)
    for url, what in ((a.github, "the GitHub token"), (a.hermes, "the Hermes key")):
        net.local_only(url, what)
    return Context(a, board.Board(a.github, ghtoken), (hkey, orkey), log)


def main(argv=None):
    a = parser().parse_args(argv)
    try:
        COMMANDS.get(a.cmd, run.run_lane)(context(a))
        return 0
    except net.Failure as e:
        print(f"dispatch: {net.redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
