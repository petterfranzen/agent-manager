#!/usr/bin/env python3
"""Crew status: what each agent is up to, for the public crew page, every hour.

The crew page in ghost-in-the-machine (`crew/`) fetches `crew/status.json` from
that repo's `crew-status` branch. Every hour, after the spend sync has booked
the hour's spend (agent-manager-crew-status.service runs after
agent-manager-spend.service), this script:
  1. reads the agents, their in-progress and done issues, their runs and their
     booked cost this month from Paperclip on loopback,
  2. reads OpenRouter's month-to-date usage for the company's budget gauge,
  3. reads merged ghost-in-the-machine PRs from the public GitHub API,
  4. writes status.json to the `crew-status` branch through the contents API,
     one small commit per change, skipping the write when nothing but
     `generatedAt` changed in the last 3 hours.

The page is public, so text is allowlisted, never blocklisted: only issues with
a public title (ALLOWED below) and merged PR titles are shown; every other task
is "On a private task" or "Research (private)", with no title. A deny-term check
on the finished document is a second line: if a private word slips through, the
script fails and publishes nothing. No money amounts, only percentages.

Runs on the VM as root. Standard library only.
Secrets come from root-only files and are never printed:
  PAPERCLIP_BOARD_KEY  in <secrets-dir>/paperclip-board.env
  OPENROUTER_API_KEY   in <secrets-dir>/hermes.env
  GH_TOKEN             in <secrets-dir>/sandbox.env (fine-grained, ghost-in-the-machine only)
"""

import argparse
import base64
import datetime as dt
import json
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

COMPANY = "Section 9"
REPO = "petterfranzen/ghost-in-the-machine"
BRANCH = "crew-status"
PATH = "crew/status.json"
PAGES = "https://petterfranzen.github.io/ghost-in-the-machine/"
URLS = (PAGES, f"https://github.com/{REPO}/pull/")
AUTHOR = {"name": "Section 9 crew status", "email": "agents@sectionnine.dev"}
KEYS = re.compile(r"(pcp_board_|sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
STALE = dt.timedelta(hours=3)
RECENT = 5
TEXT_MAX = 90

# Issue titles that may appear on the public page (exact, or prefix with "...").
ALLOWED_EXACT = {"Nightly build", "Review and redesign the crew page (GITS-95)"}
ALLOWED_PREFIX = ("Crew page", "Night ", "Check: ")
# Never published, whatever the source; a hit fails the whole run. The owner's
# name is here; the private topics are one term per line in <secrets-dir>/crew-deny.txt
# (root-only on the VM, so this public repo doesn't list them), and the run refuses
# to publish without that file.
DENY_BASE = r"petter|franz[eé]n"
DENY = re.compile(DENY_BASE, re.IGNORECASE)
PRIVATE = "On a private task"
PRIVATE_RESEARCH = "Research (private)"
IDENT = re.compile(r"\b([A-Z]{2,6}-\d+)\b")
STATES = {"running": "working", "idle": "idle", "error": "idle", "paused": "paused"}


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    """Last NAME=VALUE line in an env file; quotes stripped (as spend-report.py)."""
    value = None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, val = line.partition("=")
        if sep and key.strip() == name:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            value = val
    if not value:
        raise Failure(f"{name} is missing or empty in {path}")
    return value


def http(method, url, token=None, body=None, github=False, missing_ok=False):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json" if github else "application/json")
    if github:
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "agent-manager-crew-status")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        if missing_ok and e.code == 404:
            return None
        detail = redact(e.read().decode("utf-8", "replace"))[:300]
        raise Failure(f"{method} {redact(url)} -> {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Failure(f"{method} {redact(url)} -> no response: {redact(str(getattr(e, 'reason', e)))}") from None
    return json.loads(raw) if raw.strip() else None


def q(value):
    return urllib.parse.quote(str(value), safe="")


def ts(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def iso(value):
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def rows(reply):
    if isinstance(reply, dict):
        reply = reply.get("data") or reply.get("items") or reply.get("runs") or reply.get("issues") or []
    return reply or []


def public_title(title):
    title = (title or "").strip()
    return title in ALLOWED_EXACT or any(title.startswith(p) for p in ALLOWED_PREFIX)


def short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1].rstrip() + "…"


def issue_text(issue, researcher):
    """An issue's public text: its title if allowlisted, else a private label."""
    title = issue.get("title") or ""
    if issue.get("visibility", "open") == "open" and public_title(title) and not DENY.search(title):
        return short(title)
    return PRIVATE_RESEARCH if researcher else PRIVATE


def percent(part, whole):
    return round(100 * part / whole) if whole else 0


def validate(doc):
    """The schema the crew page (SEC-18) reads. Raises Failure on any mismatch."""
    z = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def need(ok, what):
        if not ok:
            raise Failure(f"status.json invalid: {what}")

    need(set(doc) == {"generatedAt", "budget", "agents"}, "top-level keys")
    need(z.match(doc["generatedAt"] or ""), "generatedAt")
    b = doc["budget"]
    need(set(b) == {"usedPercent", "month"}, "budget keys")
    need(isinstance(b["usedPercent"], int) and b["usedPercent"] >= 0, "budget.usedPercent")
    need(re.match(r"^\d{4}-\d\d$", b["month"] or ""), "budget.month")
    need(isinstance(doc["agents"], list), "agents")
    for a in doc["agents"]:
        need(set(a) == {"name", "role", "state", "now", "lastActiveAt", "budgetPercent", "recent"}, "agent keys")
        need(isinstance(a["name"], str) and a["name"] and isinstance(a["role"], str), "agent name/role")
        need(a["state"] in ("working", "idle", "paused", "off"), "agent state")
        need(a["now"] is None or isinstance(a["now"], str), "agent now")
        need(a["lastActiveAt"] is None or z.match(a["lastActiveAt"]), "agent lastActiveAt")
        need(isinstance(a["budgetPercent"], int) and a["budgetPercent"] >= 0, "agent budgetPercent")
        need(isinstance(a["recent"], list) and len(a["recent"]) <= RECENT, "agent recent")
        for r in a["recent"]:
            need(set(r) - {"url"} == {"at", "text"} and z.match(r["at"] or "") and isinstance(r["text"], str), "recent item")
            need("url" not in r or str(r["url"]).startswith(URLS), "recent url")
    text = json.dumps(doc, ensure_ascii=False)
    # Text, not URLs: the public Pages and PR URLs carry the repo owner's login.
    words = [doc["budget"]["month"]] + [v for a in doc["agents"] for v in
                                        [a["name"], a["role"], a["now"] or ""] + [r["text"] for r in a["recent"]]]
    need(not any(DENY.search(w) for w in words), "a private term would be published; nothing written")
    need(not re.search(r"\$\s?\d|cents|usd", text, re.IGNORECASE), "a money amount would be published")


def build(now, roster, agents, issues, runs, costs, monthly_usd, prs):
    """The status document from already-fetched data. Pure, so tests can drive it."""
    company_cents = int(roster["company"]["budget_monthly_cents"])
    by_name = {a.get("name"): a for a in agents}
    ident = {i.get("identifier"): i for i in issues if i.get("identifier")}
    nightly = {a["name"] for a in roster["agents"] if (a.get("routine") or {}).get("title") == "Nightly build"}
    spent = {}
    for c in costs:
        if c.get("agentId"):
            spent[c["agentId"]] = spent.get(c["agentId"], 0) + float(c.get("costCents") or 0)

    out = []
    for r in roster["agents"]:
        name = r["name"]
        a = by_name.get(name)
        aid = a.get("id") if a else None
        researcher = r.get("role") == "researcher"
        state = STATES.get((a or {}).get("status"), "off")
        mine = [i for i in issues if aid and i.get("assigneeAgentId") == aid]

        now_text = None
        if state == "working":
            active = sorted((i for i in mine if i.get("status") == "in_progress"),
                            key=lambda i: i.get("updatedAt") or "", reverse=True)
            now_text = issue_text(active[0], researcher) if active else (PRIVATE_RESEARCH if researcher else PRIVATE)

        times = [ts(x) for run in runs if aid and run.get("agentId") == aid
                 for x in [run.get("finishedAt") or run.get("startedAt") or run.get("createdAt")] if x]
        if a and a.get("lastHeartbeatAt"):
            times.append(ts(a["lastHeartbeatAt"]))
        last = iso(max(times)) if times else None

        recent = []
        for i in mine:
            when = i.get("completedAt") or i.get("updatedAt")
            if i.get("status") == "done" and when:
                recent.append({"at": iso(ts(when)), "text": issue_text(i, researcher)})
        for p in prs:
            head = (p.get("head") or {}).get("ref") or ""
            title = p.get("title") or ""
            if not p.get("merged_at") or DENY.search(title):
                continue
            if head.startswith("night/") and name in nightly:
                url = PAGES + head[len("night/"):] + "/"
            elif head.startswith("task/") and any(
                    (ident.get(m) or {}).get("assigneeAgentId") == aid
                    for m in IDENT.findall(title + " " + (p.get("body") or "")) if aid):
                url = p.get("html_url")
            else:
                continue
            item = {"at": iso(ts(p["merged_at"])), "text": short(title)}
            if url and url.startswith(URLS):
                item["url"] = url
            recent.append(item)
        recent.sort(key=lambda x: x["at"], reverse=True)
        # Private items carry no information past the first; keep one per run of them.
        deduped = []
        for item in recent:
            if deduped and item["text"] in (PRIVATE, PRIVATE_RESEARCH) and deduped[-1]["text"] == item["text"]:
                continue
            deduped.append(item)

        out.append({
            "name": name,
            "role": r.get("title") or r.get("role") or "",
            "state": state,
            "now": now_text,
            "lastActiveAt": last,
            "budgetPercent": percent(spent.get(aid, 0), int(r.get("budget_monthly_cents") or 0)) if aid else 0,
            "recent": deduped[:RECENT],
        })
    doc = {
        "generatedAt": iso(now),
        "budget": {"usedPercent": percent(monthly_usd * 100, company_cents), "month": now.strftime("%Y-%m")},
        "agents": out,
    }
    validate(doc)
    return doc


def same_but_time(a, b):
    strip = lambda d: {k: v for k, v in d.items() if k != "generatedAt"}
    return strip(a) == strip(b)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print the JSON, write nothing")
    p.add_argument("--secrets-dir", default="/etc/agent-manager")
    p.add_argument("--roster", default="/opt/agent-manager/agents/roster.toml")
    p.add_argument("--paperclip", default="http://127.0.0.1:3100")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    p.add_argument("--github", default="https://api.github.com", help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    try:
        host = urllib.parse.urlsplit(a.paperclip)
        if host.scheme != "http" or host.hostname not in LOOPBACK:
            raise Failure("refusing to send the board key to a non-loopback host")
        gh_host = urllib.parse.urlsplit(a.github)
        if not ((gh_host.scheme == "https" and gh_host.hostname == "api.github.com")
                or gh_host.hostname in LOOPBACK):
            raise Failure("refusing to send the GitHub token anywhere but api.github.com")
        try:
            with open(a.roster, "rb") as f:
                roster = tomllib.load(f)
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise Failure(f"cannot read the roster {a.roster}: {e}") from None
        global DENY
        try:
            terms = [re.escape(t.strip()) for t in (Path(a.secrets_dir) / "crew-deny.txt").read_text().splitlines()
                     if t.strip() and not t.startswith("#")]
        except OSError as e:
            raise Failure(f"cannot read the private deny terms: {e}") from None
        if not terms:
            raise Failure("crew-deny.txt has no terms; refusing to publish")
        DENY = re.compile("|".join([DENY_BASE, *terms]), re.IGNORECASE)
        board = read_env_value(Path(a.secrets_dir) / "paperclip-board.env", "PAPERCLIP_BOARD_KEY")
        orkey = read_env_value(Path(a.secrets_dir) / "hermes.env", "OPENROUTER_API_KEY")
        ghtok = read_env_value(Path(a.secrets_dir) / "sandbox.env", "GH_TOKEN")
        secrets.extend([board, orkey, ghtok])
        pc = a.paperclip.rstrip("/")
        gh = a.github.rstrip("/") + "/repos/" + REPO
        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0)

        k = (http("GET", a.openrouter.rstrip("/") + "/key", orkey) or {}).get("data") or {}
        if "usage_monthly" not in k:
            raise Failure("OpenRouter /key reply has no usage_monthly")

        companies = http("GET", pc + "/api/companies", board) or []
        found = [c for c in companies if c.get("name") == COMPANY and c.get("status") != "archived"]
        if len(found) != 1:
            raise Failure(f"expected one company named '{COMPANY}', found {len(found)}")
        cid = q(found[0]["id"])
        agents = rows(http("GET", f"{pc}/api/companies/{cid}/agents", board))
        issues = []
        for status in ("in_progress", "done"):
            issues += rows(http("GET", f"{pc}/api/companies/{cid}/issues?status={status}", board))
        runs = rows(http("GET", f"{pc}/api/companies/{cid}/heartbeat-runs?limit=200", board))
        costs = rows(http("GET", f"{pc}/api/companies/{cid}/costs/by-agent?from={q(iso(month_start))}"
                                 f"&to={q(iso(now))}", board))
        prs = rows(http("GET", f"{gh}/pulls?state=closed&per_page=50&sort=updated&direction=desc",
                        ghtok, github=True))

        doc = build(now, roster, agents, issues, runs, costs, float(k["usage_monthly"] or 0), prs)
        body = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"
        if a.dry_run:
            print(body, end="")
            return 0

        if http("GET", f"{gh}/git/ref/heads/{BRANCH}", ghtok, github=True, missing_ok=True) is None:
            main_ref = http("GET", f"{gh}/git/ref/heads/main", ghtok, github=True)
            http("POST", f"{gh}/git/refs", ghtok, github=True,
                 body={"ref": f"refs/heads/{BRANCH}", "sha": main_ref["object"]["sha"]})
            print(f"crew status: created branch {BRANCH}")

        current = http("GET", f"{gh}/contents/{PATH}?ref={BRANCH}", ghtok, github=True, missing_ok=True)
        sha = None
        if current:
            sha = current.get("sha")
            try:
                old = json.loads(base64.b64decode(current.get("content") or ""))
                fresh = now - ts(old["generatedAt"]) < STALE
            except (ValueError, KeyError, TypeError):
                old, fresh = None, False
            if old and fresh and same_but_time(old, doc):
                print("crew status: unchanged; no write")
                return 0
        put = {"message": f"crew status {iso(now)}", "branch": BRANCH, "author": AUTHOR, "committer": AUTHOR,
               "content": base64.b64encode(body.encode()).decode()}
        if sha:
            put["sha"] = sha
        http("PUT", f"{gh}/contents/{PATH}", ghtok, github=True, body=put)
        print(f"crew status: published {len(doc['agents'])} agents to {BRANCH}")
        return 0
    except Failure as e:
        print(f"crew status failed: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
