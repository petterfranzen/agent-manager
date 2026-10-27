#!/usr/bin/env python3
"""Apply agents/roster.toml to Paperclip through its API.

Runs on the VM host as root, against Paperclip on loopback. Safe to re-run:
it creates what is missing and changes only what differs. It never deletes.
Standard library only. See docs/RUNBOOK.md, step 7.

Secrets come from root-only files, never from arguments:
  PAPERCLIP_BOARD_KEY  in <secrets-dir>/paperclip-board.env
  API_SERVER_KEY       in <secrets-dir>/hermes.env
Neither is printed or logged.
"""

import argparse
import base64
import json
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER = "hermes_gateway"
# Paperclip's agent role enum (AGENT_ROLES in packages/shared, tag v2026.1023.0).
ROLES = {
    "ceo", "cto", "cmo", "cfo", "security", "engineer",
    "designer", "pm", "qa", "devops", "researcher", "general",
}
MAX_AVATAR_BYTES = 512 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class ApplyError(Exception):
    """Bad roster, missing secret or unexpected state. Exit 2."""


class ApiError(Exception):
    """An API call failed. Exit 1."""


def read_env_value(path, name):
    """Last NAME=VALUE line in an env file; quotes stripped."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise ApplyError(f"cannot read {path}: {e.strerror}") from None
    value = None
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
        raise ApplyError(f"{name} is missing or empty in {path}")
    return value


class Api:
    def __init__(self, base_url, board_key, redact):
        parts = urllib.parse.urlsplit(base_url)
        if parts.scheme != "http" and parts.scheme != "https":
            raise ApplyError(f"bad --base-url: {base_url}")
        if parts.scheme == "http" and parts.hostname not in LOOPBACK:
            raise ApplyError("refusing to send the board key over plain http to a non-loopback host")
        self.base = base_url.rstrip("/")
        self.key = board_key
        self.redact = redact

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self.key)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            detail = self.redact(e.read().decode("utf-8", "replace"))[:300]
            raise ApiError(f"{method} {path} -> {e.code}: {detail}") from None
        except (urllib.error.URLError, OSError) as e:
            reason = self.redact(str(getattr(e, "reason", e)))
            raise ApiError(f"{method} {path} -> no response: {reason}") from None
        if not raw.strip():
            return None
        try:
            return json.loads(raw)
        except ValueError:
            raise ApiError(f"{method} {path} -> reply is not JSON") from None


def q(value):
    return urllib.parse.quote(str(value), safe="")


def load_roster(path):
    try:
        with open(path, "rb") as f:
            roster = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ApplyError(f"cannot read roster {path}: {e}") from None

    def need(table, key, kind, where):
        value = table.get(key)
        if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
            raise ApplyError(f"roster: {where} needs {key} ({kind.__name__})")
        return table[key]

    company = roster.get("company", {})
    need(company, "name", str, "[company]")
    need(company, "budget_monthly_cents", int, "[company]")
    defaults = roster.get("defaults", {})
    for key in ("api_base_url", "paperclip_api_url", "session_key_strategy"):
        need(defaults, key, str, "[defaults]")
    need(defaults, "timeout_sec", int, "[defaults]")
    agents = roster.get("agents", [])
    if not agents:
        raise ApplyError("roster: no [[agents]]")
    seen = set()
    for a in agents:
        where = f"agent {a.get('name', '?')}"
        for key in ("name", "role", "title", "brief", "avatar"):
            need(a, key, str, where)
        need(a, "budget_monthly_cents", int, where)
        if a["role"] not in ROLES:
            raise ApplyError(f"roster: {where} has unknown role {a['role']}")
        if a["name"] in seen:
            raise ApplyError(f"roster: duplicate agent {a['name']}")
        seen.add(a["name"])
        if "timeout_sec" in a:
            need(a, "timeout_sec", int, where)
        r = a.get("routine")
        if r is not None:
            for key in ("title", "description", "cron", "timezone"):
                need(r, key, str, where + " routine")
            need(r, "active", bool, where + " routine")
            if "{{" in r["title"] + r["description"]:
                raise ApplyError(f"roster: {where} routine: braces make a Paperclip variable")
    return roster


def read_text(rel, what):
    path = ROOT / rel
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        raise ApplyError(f"cannot read {what} {rel}: {e.strerror}") from None


def read_avatar(rel):
    path = ROOT / rel
    try:
        data = path.read_bytes()
    except OSError as e:
        raise ApplyError(f"cannot read avatar {rel}: {e.strerror}") from None
    if not data.startswith(PNG_MAGIC) or len(data) > MAX_AVATAR_BYTES:
        raise ApplyError(f"avatar {rel} must be a PNG of at most 512 KiB")
    return base64.b64encode(data).decode("ascii")


class Applier:
    def __init__(self, api, roster, dry_run, set_api_key, hermes_key):
        self.api = api
        self.roster = roster
        self.dry = dry_run
        self.set_api_key = set_api_key
        self.hermes_key = hermes_key
        self.changes = 0

    def say(self, line):
        print(("[dry-run] " if self.dry else "") + line)

    def change(self, line):
        self.changes += 1
        self.say(line)

    def write(self, method, path, body):
        """The only place that sends a non-GET. A dry run never gets here."""
        if self.dry:
            return None
        return self.api.call(method, path, body)

    def run(self):
        company = self.find_company()
        cid = company["id"]
        self.sync_company(company)

        agents = [a for a in self.api.call("GET", f"/api/companies/{q(cid)}/agents") or []
                  if a.get("status") != "terminated"]
        by_name = {a["name"]: a for a in agents}
        routines = [r for r in self.api.call("GET", f"/api/companies/{q(cid)}/routines") or []
                    if r.get("status") != "archived"]

        d = self.roster["defaults"]
        for spec in self.roster["agents"]:
            brief = read_text(spec["brief"], "brief")
            config = {
                "apiBaseUrl": d["api_base_url"],
                "paperclipApiUrl": d["paperclip_api_url"],
                "sessionKeyStrategy": d["session_key_strategy"],
                "timeoutSec": spec.get("timeout_sec", d["timeout_sec"]),
                "instructions": brief,
            }
            agent = self.sync_agent(company, spec, config, by_name.get(spec["name"]))
            if agent.get("status") != "pending_approval":
                self.sync_avatar(cid, spec, agent, by_name.get(spec["name"]))
            if spec.get("routine"):
                self.sync_routine(cid, spec["routine"], agent.get("id"), routines)

        roster_names = {a["name"] for a in self.roster["agents"]}
        for name in sorted(set(by_name) - roster_names):
            self.say(f"not in roster: {name} (left alone)")

        if self.dry:
            self.say(f"{self.changes} change(s) planned; nothing written")
        else:
            print(f"done: {self.changes} change(s)")

    def find_company(self):
        name = self.roster["company"]["name"]
        found = [c for c in self.api.call("GET", "/api/companies") or []
                 if c.get("name") == name and c.get("status") != "archived"]
        if not found:
            raise ApplyError(f"company '{name}' not found; it is created when the board "
                             "is claimed (docs/RUNBOOK.md, step 6)")
        if len(found) > 1:
            raise ApplyError(f"more than one company named '{name}'")
        return found[0]

    def sync_company(self, company):
        want = self.roster["company"]["budget_monthly_cents"]
        have = company.get("budgetMonthlyCents")
        if have == want:
            self.say(f"ok: company {company['name']} budget {want} cents")
            return
        self.write("PATCH", f"/api/companies/{q(company['id'])}", {"budgetMonthlyCents": want})
        self.change(f"company {company['name']}: budget {have} -> {want} cents")

    def sync_agent(self, company, spec, config, have):
        """Create or update one agent. Returns its row (id is None in a dry run)."""
        name = spec["name"]
        if have is None:
            return self.create_agent(company, spec, config)

        if have.get("status") == "pending_approval":
            self.write("POST", f"/api/agents/{q(have['id'])}/approve", {})
            self.change(f"agent {name}: approve pending hire")
            have = dict(have, status="idle")

        cur = have.get("adapterConfig") or {}
        patch = {}
        notes = []
        for field, want in (("role", spec["role"]), ("title", spec["title"]),
                            ("budgetMonthlyCents", spec["budget_monthly_cents"])):
            if have.get(field) != want:
                patch[field] = want
                notes.append(f"{field} {have.get(field)} -> {want}")

        if have.get("adapterType") != ADAPTER:
            # A different adapter: replace the whole config and send the key.
            patch.update(adapterType=ADAPTER, replaceAdapterConfig=True,
                         adapterConfig=dict(config, apiKey=self.hermes_key))
            notes.insert(0, f"switch adapter {have.get('adapterType')} -> {ADAPTER}")
        else:
            differ = [k for k, v in config.items() if cur.get(k) != v]
            send_key = self.set_api_key or not cur.get("apiKey")
            if differ or send_key:
                # The server merges adapterConfig, so the stored key stays unless we send one.
                patch["adapterConfig"] = dict(config, apiKey=self.hermes_key) if send_key else dict(config)
                if differ:
                    notes.append("config " + ", ".join(differ))
                if send_key:
                    notes.append("api key")

        if not patch:
            self.say(f"ok: {name} unchanged")
            return have
        self.write("PATCH", f"/api/agents/{q(have['id'])}", patch)
        self.change(f"agent {name}: " + "; ".join(notes))
        return have

    def create_agent(self, company, spec, config):
        name = spec["name"]
        cid = company["id"]
        body = {
            "name": name,
            "role": spec["role"],
            "title": spec["title"],
            "adapterType": ADAPTER,
            "adapterConfig": dict(config, apiKey=self.hermes_key),
            "budgetMonthlyCents": spec["budget_monthly_cents"],
        }
        if company.get("requireBoardApprovalForNewAgents"):
            res = self.write("POST", f"/api/companies/{q(cid)}/agent-hires", body)
            agent = (res or {}).get("agent") or {}
            if res is not None and agent.get("status") == "pending_approval":
                self.write("POST", f"/api/agents/{q(agent['id'])}/approve", {})
                agent = dict(agent, status="idle")
            self.change(f"agent {name}: create (hire, approve)")
        else:
            agent = self.write("POST", f"/api/companies/{q(cid)}/agents", body) or {}
            self.change(f"agent {name}: create")
        if agent.get("name") not in (None, name):
            raise ApplyError(f"agent {name} was created as {agent['name']}: a same-named agent exists")
        return agent

    def sync_avatar(self, cid, spec, agent, before):
        name = spec["name"]
        image = read_avatar(spec["avatar"])
        old = ((before or {}).get("appearance") or {}).get("customAvatarAssetId")
        if self.dry:
            if not old:
                self.change(f"avatar {name}: upload {spec['avatar']}")
            return
        res = self.write("PUT", f"/api/companies/{q(cid)}/agents/{q(agent['id'])}/avatar",
                         {"imageBase64": image})
        new = ((res or {}).get("appearance") or {}).get("customAvatarAssetId")
        if new != old:
            self.change(f"avatar {name}: upload {spec['avatar']}")

    def sync_routine(self, cid, spec, agent_id, routines):
        title = spec["title"]
        status = "active" if spec["active"] else "paused"
        have = next((r for r in routines
                     if r.get("title") == title and agent_id and r.get("assigneeAgentId") == agent_id), None)
        if have is None:
            body = {"title": title, "description": spec["description"],
                    "assigneeAgentId": agent_id, "status": status}
            res = self.write("POST", f"/api/companies/{q(cid)}/routines", body)
            self.change(f"routine {title}: create ({status})")
            self.add_schedule(title, (res or {}).get("id"), spec)
            return

        patch = {}
        if (have.get("description") or "") != spec["description"]:
            patch["description"] = spec["description"]
        if have.get("status") != status:
            patch["status"] = status
        if patch:
            self.write("PATCH", f"/api/routines/{q(have['id'])}", patch)
            self.change(f"routine {title}: update " + ", ".join(patch))

        schedules = [t for t in have.get("triggers") or [] if t.get("kind") == "schedule"]
        if not schedules:
            self.add_schedule(title, have["id"], spec)
            return
        trig = schedules[0]
        tpatch = {}
        if trig.get("cronExpression") != spec["cron"]:
            tpatch["cronExpression"] = spec["cron"]
        if trig.get("timezone") != spec["timezone"]:
            tpatch["timezone"] = spec["timezone"]
        if trig.get("enabled") is not True:
            tpatch["enabled"] = True
        if tpatch:
            self.write("PATCH", f"/api/routine-triggers/{q(trig['id'])}", tpatch)
            self.change(f"routine {title}: update schedule " + ", ".join(tpatch))
        if len(schedules) > 1:
            self.say(f"note: routine {title} has {len(schedules)} schedules; only the first is managed")
        if not patch and not tpatch:
            self.say(f"ok: routine {title} unchanged")

    def add_schedule(self, title, routine_id, spec):
        body = {"kind": "schedule", "cronExpression": spec["cron"],
                "timezone": spec["timezone"], "enabled": True, "label": "schedule"}
        self.write("POST", f"/api/routines/{q(routine_id)}/triggers", body)
        self.change(f"routine {title}: add schedule {spec['cron']} {spec['timezone']}")


def main(argv=None):
    p = argparse.ArgumentParser(description="Apply agents/roster.toml to Paperclip.")
    p.add_argument("--roster", default=str(ROOT / "agents" / "roster.toml"))
    p.add_argument("--dry-run", action="store_true", help="print planned changes, write nothing")
    p.add_argument("--set-api-key", action="store_true",
                   help="send the Hermes key to every agent even if unchanged")
    p.add_argument("--base-url", default="http://127.0.0.1:3100", help="Paperclip URL")
    p.add_argument("--secrets-dir", default="/etc/agent-manager")
    args = p.parse_args(argv)

    secrets = []
    try:
        sdir = Path(args.secrets_dir)
        board_key = read_env_value(sdir / "paperclip-board.env", "PAPERCLIP_BOARD_KEY")
        hermes_key = read_env_value(sdir / "hermes.env", "API_SERVER_KEY")
        secrets = [board_key, hermes_key]

        def redact(text):
            for s in secrets:
                text = text.replace(s, "[redacted]")
            return text

        roster = load_roster(args.roster)
        api = Api(args.base_url, board_key, redact)
        Applier(api, roster, args.dry_run, args.set_api_key, hermes_key).run()
    except ApiError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ApplyError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
