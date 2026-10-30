#!/usr/bin/env python3
"""Spend sync: OpenRouter's real figures into Paperclip, every hour.

Hermes reports tokens but no price, so Paperclip books every agent run as
"unpriced" and its budgets see $0. Every hour this script:
  1. reads the OpenRouter key's usage (GET /api/v1/key),
  2. splits the spend since its last run across the agents by their runs'
     tokens in that window (input + 50 x output: with ~90% cache hits, output
     costs about 50 times more per token than input on our models),
  3. books each share in Paperclip as a cost event, so its budgets and
     by-agent cost views show real money,
  4. keeps a live snapshot in the open "Spend" issue's description (OpenRouter's
     month, cap, remaining, today), so anyone with Paperclip access can read the
     true figures at most an hour old, and once a day, from 07:00 Stockholm,
     posts a summary comment there.

Booking hourly keeps Paperclip's budgets current, so they stop an agent
within the hour rather than a day late. Runs on the VM as root
(agent-manager-spend.timer). Standard library only.
Secrets come from root-only files and are never printed:
  PAPERCLIP_BOARD_KEY  in <secrets-dir>/paperclip-board.env
  OPENROUTER_API_KEY   in <secrets-dir>/hermes.env
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

COMPANY = "Section 9"
TITLE = "Spend"
DESCRIPTION = "The VM books OpenRouter's real spend per agent every hour and posts a summary here once a day."
OPEN = "backlog,todo,in_progress,in_review,blocked"
OUTPUT_WEIGHT = 50
MODEL = "deepseek/deepseek-v4.1-flash"
KEYS = re.compile(r"(pcp_board_|sk-or-|github_pat_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
STOCKHOLM = ZoneInfo("Europe/Stockholm")
COMMENT_HOUR = 7


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    """Last NAME=VALUE line in an env file; quotes stripped (as paperclip-apply.py)."""
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


def http(method, url, token, body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        detail = redact(e.read().decode("utf-8", "replace"))[:300]
        raise Failure(f"{method} {redact(url)} -> {e.code}: {detail}") from None
    except (urllib.error.URLError, OSError) as e:
        raise Failure(f"{method} {redact(url)} -> no response: {redact(str(getattr(e, 'reason', e)))}") from None
    return json.loads(raw) if raw.strip() else None


def q(value):
    return urllib.parse.quote(str(value), safe="")


def ts(value):
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def usd(value):
    return f"${Decimal(value).quantize(Decimal('0.01'), ROUND_HALF_UP)}"


def split(total, weights):
    """total (Decimal USD) by weight; the last agent takes the rounding remainder."""
    weighted = {k: w for k, w in weights.items() if w > 0}
    whole = sum(weighted.values())
    if not weighted or total <= 0:
        return {}
    out, left = {}, total
    names = sorted(weighted)
    for name in names[:-1]:
        share = (total * weighted[name] / whole).quantize(Decimal("0.000001"))
        out[name] = share
        left -= share
    out[names[-1]] = left
    return out


def snapshot(k, now, local, booked, pending, agents):
    """The Spend issue's description: OpenRouter's own figures, then what is booked."""
    def money(key):
        return usd(k[key]) if k.get(key) is not None else "n/a"
    out = [
        f"**Live snapshot, updated hourly by the VM.** As of {now:%Y-%m-%d %H:%M} UTC ({local:%H:%M} Stockholm).",
        "",
        "OpenRouter (the key the agents use; true cost):",
        f"- This month (UTC): {money('usage_monthly')}",
        f"- Cap: {money('limit')}" + (f" ({k['limit_reset']} reset)" if k.get("limit_reset") else ""),
        f"- Left: {money('limit_remaining')}",
        f"- Today (UTC): {money('usage_daily')}; this week: {money('usage_weekly')}; all time: {money('usage')}",
        "",
        "Booked in Paperclip since the last daily summary:",
    ]
    if booked:
        out += [f"- {agents.get(aid, aid)}: {usd(v)}" for aid, v in sorted(booked.items(), key=lambda kv: -kv[1])]
    else:
        out.append("- nothing yet")
    if pending:
        out.append(f"- waiting for a finished run: {usd(pending)}")
    out += ["", DESCRIPTION]
    return "\n".join(out)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print, write nothing")
    p.add_argument("--secrets-dir", default="/etc/agent-manager")
    p.add_argument("--state", default="/var/lib/agent-manager/spend-state.json")
    p.add_argument("--paperclip", default="http://127.0.0.1:3100")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    p.add_argument("--comment", action="store_true", help="post the summary comment now")
    p.add_argument("--comment-hour", type=int, default=COMMENT_HOUR, help=argparse.SUPPRESS)
    a = p.parse_args(argv)

    try:
        host = urllib.parse.urlsplit(a.paperclip)
        if host.scheme != "http" or host.hostname not in LOOPBACK:
            raise Failure("refusing to send the board key to a non-loopback host")
        board = read_env_value(Path(a.secrets_dir) / "paperclip-board.env", "PAPERCLIP_BOARD_KEY")
        orkey = read_env_value(Path(a.secrets_dir) / "hermes.env", "OPENROUTER_API_KEY")
        secrets.extend([board, orkey])
        pc = a.paperclip.rstrip("/")

        k = (http("GET", a.openrouter.rstrip("/") + "/key", orkey) or {}).get("data") or {}
        if "usage" not in k:
            raise Failure("OpenRouter /key reply has no usage")
        usage = Decimal(str(k["usage"]))
        now = dt.datetime.now(dt.timezone.utc)

        state_path = Path(a.state)
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            state = {}
        since = ts(state["at"]) if state.get("at") else now - dt.timedelta(days=1)
        # First run: book nothing; earlier spend predates the reporting.
        delta = usage - Decimal(str(state["usage"])) if "usage" in state else Decimal(0)
        if delta < 0:
            delta = Decimal(0)

        companies = http("GET", pc + "/api/companies", board) or []
        found = [c for c in companies if c.get("name") == COMPANY and c.get("status") != "archived"]
        if len(found) != 1:
            raise Failure(f"expected one company named '{COMPANY}', found {len(found)}")
        cid = found[0]["id"]
        agents = {x["id"]: x["name"] for x in http("GET", f"{pc}/api/companies/{q(cid)}/agents", board) or []}

        runs = http("GET", f"{pc}/api/companies/{q(cid)}/heartbeat-runs?limit=500", board) or []
        if isinstance(runs, dict):
            runs = runs.get("runs") or runs.get("data") or []
        tokens = {}
        for r in runs:
            end = r.get("finishedAt") or r.get("updatedAt") or r.get("createdAt")
            if not end or not (since < ts(end) <= now) or r.get("agentId") not in agents:
                continue
            t = tokens.setdefault(r["agentId"], [0, 0])
            t[0] += int(r.get("inputTokens") or 0)
            t[1] += int(r.get("outputTokens") or 0)
        shares = split(delta, {aid: i + OUTPUT_WEIGHT * o for aid, (i, o) in tokens.items()})

        local = now.astimezone(STOCKHOLM)
        booked = {aid: Decimal(v) for aid, v in (state.get("booked") or {}).items()}
        for aid, share in shares.items():
            booked[aid] = booked.get(aid, Decimal(0)) + share
        # One summary comment a day, on the first run from 07:00 Stockholm (before
        # the 07:47 check-in); the hourly runs in between only book.
        comment = a.comment or (local.hour >= a.comment_hour
                                and state.get("commentedOn") != local.date().isoformat())

        lines = [f"Spend report {local:%Y-%m-%d %H:%M} Stockholm", ""]
        limit = k.get("limit")
        month = Decimal(str(k.get("usage_monthly") or 0))
        cap = f" of {usd(limit)} cap" if limit else ""
        left = f", {usd(k['limit_remaining'])} left" if k.get("limit_remaining") is not None else ""
        lines.append(f"OpenRouter this month (UTC): {usd(month)}{cap}{left}. Today so far: {usd(k.get('usage_daily') or 0)}.")
        if "usage" not in state:
            lines.append("First report: nothing booked yet; from the next hour, spend is booked per agent every hour.")
        else:
            since_c = state.get("commentAt") or state.get("at")
            total = sum(booked.values(), Decimal(0))
            lines.append(f"Booked in Paperclip since the last report ({ts(since_c):%Y-%m-%d %H:%M} UTC): {usd(total)}"
                         + (":" if booked else "."))
            for aid, amount in sorted(booked.items(), key=lambda kv: -kv[1]):
                lines.append(f"- {agents.get(aid, aid)}: {usd(amount)}")
            pending = delta if delta > 0 and not shares else Decimal(0)
            if pending:
                lines.append(f"Waiting to be booked (no finished run yet): {usd(pending)}.")

        if a.dry_run:
            print("\n".join(lines))
            print(f"(would book {usd(sum(shares.values(), Decimal(0)))} across {len(shares)} agent(s); "
                  f"comment: {'yes' if comment else 'no'})")
            return 0

        hour = now.strftime("%Y-%m-%dT%H")
        for aid, share in shares.items():
            i, o = tokens[aid]
            http("POST", f"{pc}/api/companies/{q(cid)}/cost-events", board, {
                "idempotencyKey": f"openrouter-{hour}-{aid}",
                "agentId": aid,
                "provider": "openrouter",
                "biller": "openrouter",
                "billingType": "metered_api",
                "costStatus": "reported",
                "model": MODEL,
                "inputTokens": i,
                "outputTokens": o,
                "costCents": str(share * 100),
                "occurredAt": now.isoformat().replace("+00:00", "Z"),
            })

        issues = http("GET", f"{pc}/api/companies/{q(cid)}/issues?status={q(OPEN)}", board) or []
        issue = next((i for i in issues if i.get("title") == TITLE), None)
        if issue is None:
            issue = http("POST", f"{pc}/api/companies/{q(cid)}/issues", board, {
                "title": TITLE, "description": DESCRIPTION, "status": "backlog",
                "priority": "low", "allowDuplicate": True})
        # Every run: the issue description is the live snapshot anyone with
        # Paperclip access can read (the Major, the check-in), at most an hour old.
        http("PATCH", f"{pc}/api/issues/{q(issue['id'])}", board,
             {"description": snapshot(k, now, local, booked, delta if delta > 0 and not shares else Decimal(0), agents)})
        if comment:
            http("POST", f"{pc}/api/issues/{q(issue['id'])}/comments", board, {"body": "\n".join(lines)})

        # Spend with no finished run to book it to stays pending: a later run books it.
        new_state = dict(state)
        if not (delta > 0 and not shares):
            new_state.update(usage=str(usage), at=now.isoformat())
        new_state["booked"] = {} if comment else {aid: str(v) for aid, v in booked.items()}
        if comment:
            new_state.update(commentedOn=local.date().isoformat(), commentAt=now.isoformat())
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(new_state))
        os.replace(tmp, state_path)
        if delta > 0 and not shares:
            print(f"spend sync: {usd(delta)} carried over (no agent runs yet)")
        else:
            print(f"spend sync: {usd(delta)} booked across {len(shares)} agent(s)")
        return 0
    except Failure as e:
        print(f"spend report failed: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
