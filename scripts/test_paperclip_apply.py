"""Tests for paperclip-apply.py against a small fake Paperclip.

Run: python3 -m unittest scripts/test_paperclip_apply.py
The fake implements only the endpoints the script uses, with the response
shapes of Paperclip v2026.1023.0. Budgets follow its rules: a company or agent
budget creates and mirrors a policy (unpriced usage "block" by default), saving
a policy releases the budget holds and the budget pause it owns, and nothing
ever releases a hold set by hand.
"""

import base64
import copy
import hashlib
import json
import re
import subprocess
import sys
import tempfile
import threading
import tomllib
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "paperclip-apply.py"
REDACTED = "***REDACTED***"
# What the roster asks each routine to be, so the tests follow roster.toml.
WANT = {a["routine"]["title"]: ("active" if a["routine"]["active"] else "paused")
        for a in tomllib.loads((ROOT / "agents" / "roster.toml").read_text())["agents"] if a.get("routine")}


class FakePaperclip:
    """In-memory Paperclip. `log` holds every request; `mutations` counts real state changes."""

    def __init__(self, board_key, require_approval=False):
        self.board_key = board_key
        self.log = []
        self.mutations = 0
        self.fail = {}  # (method, path regex) -> (status, body)
        self.company = {
            "id": str(uuid.uuid4()), "name": "Section 9", "status": "active",
            "budgetMonthlyCents": 0, "requireBoardApprovalForNewAgents": require_approval,
            "pauseReason": None,
        }
        self.companies = [self.company]
        self.agents = {}
        self.secrets = {}   # agent id -> stored api key
        self.avatars = {}   # agent id -> sha256
        self.routines = {}
        self.triggers = {}
        self.policies = {}  # (scope type, scope id) -> monthly policy
        self.unpriced_events = {}  # scope id -> unpriced cost events this month
        self.spent = {}  # scope id -> billed cents this month
        self.expose_holds = True
        self.projects = []
        self.keys = {}  # agent id -> [key rows]; tokens are only in mint responses

    def seed_agent(self, name, adapter_type="hermes_gateway", config=None, **fields):
        aid = str(uuid.uuid4())
        row = {"id": aid, "companyId": self.company["id"], "name": name, "role": "general",
               "title": None, "status": "idle", "adapterType": adapter_type,
               "adapterConfig": config or {}, "budgetMonthlyCents": 0, "appearance": {},
               "lifecycleHolds": [], "pauseReason": None}
        row.update(fields)
        self.agents[aid] = row
        return row

    def view_agent(self, row):
        row = copy.deepcopy(row)
        if "apiKey" in row["adapterConfig"]:
            row["adapterConfig"]["apiKey"] = REDACTED
        if not self.expose_holds:
            del row["lifecycleHolds"]
        return row

    def upsert_policy(self, scope, scope_id, amount, unpriced=None, active=None):
        key = (scope, scope_id)
        pol = self.policies.setdefault(key, {"unpricedUsagePolicy": "block", "isActive": True})
        pol.update(scopeType=scope, scopeId=scope_id, metric="billed_cents",
                   windowKind="calendar_month_utc", amount=amount)
        if unpriced is not None:
            pol["unpricedUsagePolicy"] = unpriced
        pol["isActive"] = amount > 0 and (pol["isActive"] if active is None else active)
        if scope == "company":
            self.company["budgetMonthlyCents"] = amount
        elif scope_id in self.agents:
            self.agents[scope_id]["budgetMonthlyCents"] = amount
        self.mutations += 1
        self.reconcile()

    def blocks(self, scope_id, pol):
        if not pol["isActive"] or pol["amount"] <= 0:
            return False
        unpriced = pol["unpricedUsagePolicy"] != "allow" and self.unpriced_events.get(scope_id, 0) > 0
        return self.spent.get(scope_id, 0) >= pol["amount"] or unpriced

    def reconcile(self):
        """What the server does after a policy change: lift the budget pause it owns."""
        c = self.company
        pol = self.policies.get(("company", c["id"]))
        if c["status"] == "paused" and c["pauseReason"] == "budget" and not (pol and self.blocks(c["id"], pol)):
            c["status"], c["pauseReason"] = "active", None
        for a in self.agents.values():
            if a["status"] in ("pending_approval", "terminated"):
                continue
            pol = self.policies.get(("agent", a["id"]))
            holds = [h for h in a["lifecycleHolds"] if h not in ("budget", "company_paused")]
            if pol and self.blocks(a["id"], pol):
                holds.append("budget")
            if c["status"] == "paused":
                holds.append("company_paused")
            a["lifecycleHolds"] = holds
            a["pauseReason"] = holds[0] if holds else None
            a["status"] = "paused" if holds else ("idle" if a["status"] == "paused" else a["status"])

    def view_routine(self, r):
        out = dict(r)
        out["triggers"] = [dict(t) for t in self.triggers.values() if t["routineId"] == r["id"]]
        return out

    # Each handler returns (status, body).
    def handle(self, method, path, body):
        for m, pattern, fn in self.routes:
            if m == method:
                hit = re.fullmatch(pattern, path)
                if hit:
                    return fn(self, body, *hit.groups())
        return 404, {"error": "no such route"}

    def h_companies(self, body):
        return 200, self.companies

    def h_patch_company(self, body, cid):
        if body.keys() - {"budgetMonthlyCents"}:
            return 400, {"error": "unexpected field"}
        self.company.update(body)
        self.mutations += 1
        self.upsert_policy("company", cid, body["budgetMonthlyCents"])
        return 200, self.company

    def h_agents(self, body, cid):
        return 200, [self.view_agent(a) for a in self.agents.values()]

    def _new_agent(self, body, status):
        if not isinstance(body.get("adapterConfig", {}).get("apiKey"), str):
            return 422, {"error": "apiKey required"}
        row = self.seed_agent(body["name"], body["adapterType"],
                              dict(body["adapterConfig"]), role=body["role"], title=body["title"],
                              budgetMonthlyCents=body["budgetMonthlyCents"], status=status)
        self.secrets[row["id"]] = body["adapterConfig"]["apiKey"]
        self.mutations += 1
        if body["budgetMonthlyCents"] > 0:
            self.upsert_policy("agent", row["id"], body["budgetMonthlyCents"])
        return row

    def h_create_agent(self, body, cid):
        if self.company["requireBoardApprovalForNewAgents"]:
            return 409, {"error": "Direct agent creation requires board approval."}
        row = self._new_agent(body, "idle")
        return (201, self.view_agent(row)) if isinstance(row, dict) else row

    def h_hire(self, body, cid):
        pending = self.company["requireBoardApprovalForNewAgents"]
        row = self._new_agent(body, "pending_approval" if pending else "idle")
        if not isinstance(row, dict) or "id" not in row:
            return row
        return 201, {"agent": self.view_agent(row), "approval": {"id": str(uuid.uuid4())} if pending else None}

    def h_approve(self, body, aid):
        row = self.agents.get(aid)
        if not row or row["status"] != "pending_approval":
            return 409, {"error": "Only pending approval agents can be approved"}
        row["status"] = "idle"
        self.mutations += 1
        return 200, self.view_agent(row)

    def h_patch_agent(self, body, aid):
        row = self.agents.get(aid)
        if not row:
            return 404, {"error": "Agent not found"}
        body = dict(body)
        replace = body.pop("replaceAdapterConfig", False)
        config = body.pop("adapterConfig", None)
        changing = "adapterType" in body and body["adapterType"] != row["adapterType"]
        if config is not None:
            if "apiKey" in config:
                self.secrets[aid] = config["apiKey"]
            merged = config if (replace or changing) else {**row["adapterConfig"], **config}
            if "apiKey" not in config and "apiKey" in row["adapterConfig"] and not (replace or changing):
                merged["apiKey"] = row["adapterConfig"]["apiKey"]
            row["adapterConfig"] = merged
        row.update(body)
        self.mutations += 1
        if "budgetMonthlyCents" in body:
            self.upsert_policy("agent", aid, body["budgetMonthlyCents"])
        return 200, self.view_agent(row)

    def h_overview(self, body, cid):
        out = []
        for (scope, scope_id), pol in self.policies.items():
            out.append(dict(pol, amount=pol["amount"] if pol["isActive"] else 0))
        return 200, {"companyId": cid, "policies": out, "activeIncidents": []}

    def h_policy(self, body, cid):
        scope, scope_id = body["scopeType"], body["scopeId"]
        known = scope_id == self.company["id"] if scope == "company" else scope_id in self.agents
        if not known:
            return 404, {"error": "Budget scope not found"}
        if body.get("metric") != "billed_cents" or body.get("windowKind") != "calendar_month_utc":
            return 400, {"error": "unexpected policy key"}
        self.upsert_policy(scope, scope_id, body["amount"], body.get("unpricedUsagePolicy"), body.get("isActive"))
        return 200, self.policies[(scope, scope_id)]

    def h_avatar(self, body, cid, aid):
        row = self.agents.get(aid)
        if not row:
            return 404, {"error": "Agent not found"}
        # As the server: refused for these agent states and whenever the company isn't active.
        if row["status"] in ("pending_approval", "terminated") or self.company["status"] != "active":
            return 409, {"error": "Agent avatar cannot be updated in its current state"}
        raw = base64.b64decode(body["imageBase64"], validate=True)
        digest = hashlib.sha256(raw).hexdigest()
        if self.avatars.get(aid) != digest:
            self.avatars[aid] = digest
            row["appearance"] = {"customAvatarAssetId": str(uuid.uuid4())}
            self.mutations += 1
        return 200, {"agentId": aid, "appearance": row["appearance"]}

    def h_routines(self, body, cid):
        return 200, [self.view_routine(r) for r in self.routines.values()]

    def h_create_routine(self, body, cid):
        if body["status"] not in ("active", "paused"):
            return 400, {"error": "bad status"}
        rid = str(uuid.uuid4())
        self.routines[rid] = {"id": rid, "companyId": cid, "title": body["title"],
                              "description": body["description"], "assigneeAgentId": body["assigneeAgentId"],
                              "status": body["status"]}
        self.mutations += 1
        return 201, self.routines[rid]

    def h_patch_routine(self, body, rid):
        if rid not in self.routines:
            return 404, {"error": "Routine not found"}
        self.routines[rid].update(body)
        self.mutations += 1
        return 200, self.routines[rid]

    def h_create_trigger(self, body, rid):
        if rid not in self.routines or body.get("kind") != "schedule":
            return 400, {"error": "bad trigger"}
        tid = str(uuid.uuid4())
        self.triggers[tid] = {"id": tid, "routineId": rid, "kind": "schedule", "label": body["label"],
                              "enabled": body["enabled"], "cronExpression": body["cronExpression"],
                              "timezone": body["timezone"]}
        self.mutations += 1
        return 201, {"trigger": self.triggers[tid], "revision": {"id": str(uuid.uuid4())}}

    def h_patch_trigger(self, body, tid):
        if tid not in self.triggers:
            return 404, {"error": "Routine trigger not found"}
        self.triggers[tid].update(body)
        self.mutations += 1
        return 200, self.triggers[tid]

    def h_projects(self, body, cid):
        return 200, self.projects

    def h_create_project(self, body, cid):
        row = {"id": str(uuid.uuid4()), "companyId": cid, "archivedAt": None, **body}
        self.projects.append(row)
        self.mutations += 1
        return 201, row

    def h_keys(self, body, aid):
        return 200, [{k: v for k, v in row.items() if k != "token"} for row in self.keys.get(aid, [])]

    def h_create_key(self, body, aid):
        scope = body.get("scope") or {"kind": "standard"}
        if scope.get("kind") == "task_bridge" and not (scope.get("projectId") or scope.get("parentIssueId")):
            return 400, {"error": "task_bridge keys require at least one project or parent issue boundary"}
        row = {"id": str(uuid.uuid4()), "name": body["name"], "scope": scope,
               "revokedAt": None, "token": "pcp_" + uuid.uuid4().hex}
        self.keys.setdefault(aid, []).append(row)
        self.mutations += 1
        return 201, dict(row)

    def h_revoke_key(self, body, aid, kid):
        for row in self.keys.get(aid, []):
            if row["id"] == kid:
                row["revokedAt"] = "2026-10-28T00:00:00Z"
                self.mutations += 1
                return 200, {"ok": True}
        return 404, {"error": "Key not found"}

    def live_keys(self, aid):
        return [k for k in self.keys.get(aid, []) if not k["revokedAt"]]

    routes = [
        ("GET", r"/api/companies", h_companies),
        ("PATCH", r"/api/companies/([^/]+)", h_patch_company),
        ("GET", r"/api/companies/([^/]+)/agents", h_agents),
        ("POST", r"/api/companies/([^/]+)/agents", h_create_agent),
        ("POST", r"/api/companies/([^/]+)/agent-hires", h_hire),
        ("POST", r"/api/agents/([^/]+)/approve", h_approve),
        ("PATCH", r"/api/agents/([^/]+)", h_patch_agent),
        ("GET", r"/api/companies/([^/]+)/budgets/overview", h_overview),
        ("POST", r"/api/companies/([^/]+)/budgets/policies", h_policy),
        ("PUT", r"/api/companies/([^/]+)/agents/([^/]+)/avatar", h_avatar),
        ("GET", r"/api/companies/([^/]+)/routines", h_routines),
        ("POST", r"/api/companies/([^/]+)/routines", h_create_routine),
        ("PATCH", r"/api/routines/([^/]+)", h_patch_routine),
        ("POST", r"/api/routines/([^/]+)/triggers", h_create_trigger),
        ("PATCH", r"/api/routine-triggers/([^/]+)", h_patch_trigger),
        ("GET", r"/api/companies/([^/]+)/projects", h_projects),
        ("POST", r"/api/companies/([^/]+)/projects", h_create_project),
        ("GET", r"/api/agents/([^/]+)/keys", h_keys),
        ("POST", r"/api/agents/([^/]+)/keys", h_create_key),
        ("DELETE", r"/api/agents/([^/]+)/keys/([^/]+)", h_revoke_key),
    ]


def serve(fake):
    class Handler(BaseHTTPRequestHandler):
        def do_any(self):
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b""
            body = json.loads(raw) if raw else None
            fake.log.append({"method": self.command, "path": self.path, "body": body,
                             "auth": self.headers.get("Authorization")})
            if self.headers.get("Authorization") != "Bearer " + fake.board_key:
                status, out = 401, {"error": "Board access required"}
            else:
                for (method, pattern), (code, text) in fake.fail.items():
                    if method == self.command and re.fullmatch(pattern, self.path):
                        status, out = code, {"error": text}
                        break
                else:
                    status, out = fake.handle(self.command, self.path, body)
            data = json.dumps(out).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_any

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.board_key = "board-" + uuid.uuid4().hex
        self.hermes_key = "hermes-" + uuid.uuid4().hex
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        secrets = Path(self.tmp.name)
        (secrets / "paperclip-board.env").write_text(
            f"# board key\nPAPERCLIP_BOARD_KEY=\"{self.board_key}\"\n")
        (secrets / "hermes.env").write_text(
            f"OPENROUTER_API_KEY=unrelated\nAPI_SERVER_KEY={self.hermes_key}\n")
        self.secrets_dir = str(secrets)
        self.fake = FakePaperclip(self.board_key)
        self.start()

    def start(self):
        self.server = serve(self.fake)
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def apply(self, *flags):
        proc = subprocess.run(
            [sys.executable, "-I", str(SCRIPT), "--base-url", self.url,
             "--secrets-dir", self.secrets_dir, *flags],
            capture_output=True, text=True, timeout=60)
        self.last = proc
        return proc

    def apply_ok(self, *flags):
        proc = self.apply(*flags)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def writes(self, since=0):
        return [r for r in self.fake.log[since:] if r["method"] != "GET"]

    def key_sends(self, since=0):
        return [r for r in self.fake.log[since:]
                if r["method"] != "GET" and "apiKey" in ((r["body"] or {}).get("adapterConfig") or {})]

    def agent(self, name):
        return next(a for a in self.fake.agents.values() if a["name"] == name)

    def key_file(self, name):
        return Path(self.secrets_dir) / "bridge-keys" / f"{name.lower()}.key"

    def test_bridge_keys_minted_once_into_files(self):
        out = self.apply_ok().stdout
        self.assertEqual([p["name"] for p in self.fake.projects], ["Agent work"])
        project = self.fake.projects[0]["id"]
        for a in self.fake.agents.values():
            keys = self.fake.live_keys(a["id"])
            self.assertEqual(len(keys), 1, a["name"])
            self.assertEqual(keys[0]["scope"], {"kind": "task_bridge", "projectId": project})
            path = self.key_file(a["name"])
            self.assertEqual(path.read_text(), keys[0]["token"] + "\n")
            self.assertEqual(path.stat().st_mode & 0o777, 0o400)
            self.assertNotIn(keys[0]["token"], out + self.last.stderr)
        self.assertEqual(self.key_file("x").parent.stat().st_mode & 0o777, 0o711)

        before = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertIn("ok: bridge key Batou", out)
        self.assertEqual(self.writes(before), [w for w in self.writes(before) if "/keys" not in w["path"]])
        self.assertFalse([w for w in self.writes(before) if "/projects" in w["path"]])

    def test_bridge_key_reminted_when_file_lost(self):
        self.apply_ok()
        batou = self.agent("Batou")["id"]
        old = self.fake.live_keys(batou)[0]
        self.key_file("Batou").unlink()
        out = self.apply_ok().stdout
        self.assertIn("bridge key Batou: mint (no key file), revoked 1", out)
        self.assertIsNotNone(old["revokedAt"])
        new = self.fake.live_keys(batou)
        self.assertEqual(len(new), 1)
        self.assertEqual(self.key_file("Batou").read_text(), new[0]["token"] + "\n")
        self.assertEqual(len(self.fake.live_keys(self.agent("Togusa")["id"])), 1)

    def test_bridge_keys_dry_run_writes_nothing(self):
        self.apply_ok()
        self.fake.keys.clear()
        for p in self.key_file("x").parent.iterdir():
            p.unlink()
        out = self.apply_ok("--dry-run").stdout
        self.assertIn("bridge key Tachikoma: mint (new)", out)
        self.assertEqual(self.fake.keys, {})
        self.assertEqual(list(self.key_file("x").parent.iterdir()), [])

    def test_first_run_creates_everything(self):
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["budgetMonthlyCents"], 1900)
        self.assertEqual(sorted(a["name"] for a in self.fake.agents.values()),
                         ["Batou", "Ishikawa", "Saito", "Tachikoma", "Togusa"])
        tachikoma = self.agent("Tachikoma")
        self.assertEqual(tachikoma["role"], "engineer")
        self.assertEqual(tachikoma["title"], "Nightly builder")
        self.assertEqual(tachikoma["budgetMonthlyCents"], 1000)
        cfg = tachikoma["adapterConfig"]
        self.assertEqual(cfg["timeoutSec"], 10800)
        self.assertEqual(cfg["apiBaseUrl"], "http://127.0.0.1:8642")
        self.assertEqual(cfg["paperclipApiUrl"], "http://127.0.0.1:3100")
        self.assertEqual(cfg["sessionKeyStrategy"], "issue")
        self.assertEqual(cfg["instructions"], (ROOT / "agents/nightly-builder.md").read_text())
        self.assertEqual(self.agent("Togusa")["adapterConfig"]["timeoutSec"], 600)
        self.assertEqual(self.fake.secrets[tachikoma["id"]], self.hermes_key)
        self.assertEqual(len(self.fake.avatars), 5)

        self.assertEqual(len(self.fake.routines), 3)
        self.assertEqual({r["title"]: r["status"] for r in self.fake.routines.values()}, WANT)
        by_title = {r["title"]: r for r in self.fake.routines.values()}
        saito = by_title["Morning ops report"]
        self.assertEqual(saito["assigneeAgentId"], self.agent("Saito")["id"])
        self.assertEqual(saito["description"], "Write the morning ops report, following your brief.")
        trig = [t for t in self.fake.triggers.values() if t["routineId"] == saito["id"]]
        self.assertEqual([(t["cronExpression"], t["timezone"], t["enabled"]) for t in trig],
                         [("30 6 * * *", "Europe/Stockholm", True)])
        self.assertIn("agent Tachikoma: create", out)
        self.assertIn(f"routine Nightly build: create ({WANT['Nightly build']})", out)
        self.assertEqual(len(self.key_sends()), 5)

    def test_second_run_changes_nothing_and_sends_no_key(self):
        self.apply_ok()
        state = copy.deepcopy((self.fake.agents, self.fake.routines, self.fake.triggers, self.fake.company,
                               self.fake.policies))
        mutations, mark = self.fake.mutations, len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.mutations, mutations)
        self.assertEqual(state, (self.fake.agents, self.fake.routines, self.fake.triggers, self.fake.company,
                                 self.fake.policies))
        self.assertEqual(self.key_sends(mark), [])
        # The only non-GET calls are avatar uploads, which the server ignores when unchanged.
        self.assertTrue(all(r["method"] == "PUT" and r["path"].endswith("/avatar")
                            for r in self.writes(mark)))
        self.assertIn("ok: Batou unchanged", out)
        self.assertIn("ok: routine Nightly build unchanged", out)
        self.assertNotIn("create", out)
        self.assertIn("ok: budget policies unchanged (unpriced allow)", out)
        self.assertIn("done: 0 change(s)", out)

    def test_set_api_key_sends_it_to_every_agent(self):
        self.apply_ok()
        mark = len(self.fake.log)
        out = self.apply_ok("--set-api-key").stdout
        self.assertEqual(len(self.key_sends(mark)), 5)
        self.assertIn("agent Batou: api key", out)

    def test_changes_are_sent_without_the_key(self):
        self.apply_ok()
        batou = self.agent("Batou")
        batou["budgetMonthlyCents"] = 999
        batou["adapterConfig"]["instructions"] = "old brief"
        routine = next(r for r in self.fake.routines.values() if r["title"] == "Nightly build")
        trig = next(t for t in self.fake.triggers.values() if t["routineId"] == routine["id"])
        trig["cronExpression"] = "0 2 * * *"
        routine["status"] = "paused" if WANT[routine["title"]] == "active" else "active"
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(batou["budgetMonthlyCents"], 400)
        self.assertEqual(batou["adapterConfig"]["instructions"], (ROOT / "agents/coder.md").read_text())
        self.assertEqual(batou["adapterConfig"]["apiKey"], self.hermes_key)
        self.assertEqual(self.key_sends(mark), [])
        self.assertEqual(trig["cronExpression"], "0 1 * * *")
        self.assertEqual(routine["status"], WANT[routine["title"]])
        self.assertIn("agent Batou: budgetMonthlyCents 999 -> 400; config instructions", out)
        self.assertEqual(len([r for r in self.writes(mark) if r["path"].startswith("/api/agents/")]), 1)

    def test_saito_is_switched_to_the_gateway(self):
        old = self.fake.seed_agent("Saito", "claude_local", {"model": "claude-sonnet", "cwd": "/x"},
                                   role="devops", title="Ops", budgetMonthlyCents=100)
        out = self.apply_ok().stdout
        self.assertIn("agent Saito: switch adapter claude_local -> hermes_gateway", out)
        self.assertEqual(old["adapterType"], "hermes_gateway")
        self.assertEqual(old["title"], "Ops (read-only)")
        self.assertNotIn("model", old["adapterConfig"])
        self.assertEqual(old["adapterConfig"]["instructions"], (ROOT / "agents/ops.md").read_text())
        patches = [r for r in self.fake.log if r["method"] == "PATCH" and r["path"] == f"/api/agents/{old['id']}"]
        self.assertEqual(len(patches), 1)
        self.assertTrue(patches[0]["body"]["replaceAdapterConfig"])
        self.assertEqual(patches[0]["body"]["adapterType"], "hermes_gateway")
        sends = [r for r in self.key_sends() if r["method"] == "PATCH"]
        self.assertEqual(len(sends), 1)
        # Once switched, a re-run leaves Saito alone.
        mark = len(self.fake.log)
        self.apply_ok()
        self.assertEqual(self.key_sends(mark), [])

    def test_dry_run_writes_nothing(self):
        out = self.apply_ok("--dry-run").stdout
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.fake.agents, {})
        self.assertIn("[dry-run] agent Tachikoma: create", out)
        self.assertIn(f"[dry-run] routine Nightly build: create ({WANT['Nightly build']})", out)
        self.apply_ok()
        mark = len(self.fake.log)
        self.agent("Batou")["budgetMonthlyCents"] = 1
        self.apply_ok("--dry-run")
        self.assertEqual(self.writes(mark), [])
        self.assertEqual(self.agent("Batou")["budgetMonthlyCents"], 1)

    def policy_writes(self, since=0):
        return [r for r in self.writes(since) if r["path"].endswith("/budgets/policies")]

    def test_first_run_sets_a_policy_for_the_company_and_every_agent(self):
        out = self.apply_ok().stdout
        policies = self.fake.policies
        self.assertEqual(len(policies), 6)
        cid = self.fake.company["id"]
        self.assertEqual((policies[("company", cid)]["amount"], policies[("company", cid)]["unpricedUsagePolicy"]),
                         (1900, "allow"))
        for name, cents in (("Tachikoma", 1000), ("Batou", 400), ("Ishikawa", 300), ("Togusa", 200), ("Saito", 100)):
            pol = policies[("agent", self.agent(name)["id"])]
            self.assertEqual((pol["amount"], pol["unpricedUsagePolicy"], pol["isActive"]), (cents, "allow", True), name)
        self.assertIn("policy company Section 9: unpriced block -> allow", out)
        self.assertIn("policy agent Tachikoma: unpriced block -> allow", out)
        body = self.policy_writes()[0]["body"]
        self.assertEqual(body, {"scopeType": "company", "scopeId": cid, "metric": "billed_cents",
                                "windowKind": "calendar_month_utc", "amount": 1900, "isActive": True,
                                "unpricedUsagePolicy": "allow"})

    def test_policy_drift_is_fixed_and_a_rerun_writes_none(self):
        self.apply_ok()
        batou = self.agent("Batou")["id"]
        self.fake.policies[("agent", batou)].update(unpricedUsagePolicy="block", amount=1)
        self.fake.policies[("company", self.fake.company["id"])]["isActive"] = False
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertIn("policy agent Batou: amount 1 -> 400 cents; unpriced block -> allow", out)
        self.assertIn("policy company Section 9: amount 0 -> 1900 cents", out)
        self.assertEqual(len(self.policy_writes(mark)), 2)
        self.assertEqual(self.fake.policies[("agent", batou)]["unpricedUsagePolicy"], "allow")
        self.assertTrue(self.fake.policies[("company", self.fake.company["id"])]["isActive"])
        mark = len(self.fake.log)
        self.apply_ok()
        self.assertEqual(self.policy_writes(mark), [])

    def block_everything(self, skip=()):
        """Tachikoma ran once: unpriced usage, so the default policies paused the company and agents."""
        self.fake.company.update(status="paused", pauseReason="budget")
        for a in self.fake.agents.values():
            if a["name"] not in skip:
                self.fake.unpriced_events[a["id"]] = 1
        self.fake.unpriced_events[self.fake.company["id"]] = 1
        for key, pol in self.fake.policies.items():
            pol["unpricedUsagePolicy"] = "block"
        self.fake.reconcile()

    def test_budget_paused_company_and_agents_are_resumed(self):
        self.apply_ok()
        self.block_everything()
        self.assertEqual(self.fake.company["status"], "paused")
        self.assertTrue(all(a["status"] == "paused" for a in self.fake.agents.values()))
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["status"], "active")
        self.assertIsNone(self.fake.company["pauseReason"])
        self.assertTrue(all(a["status"] == "idle" and a["lifecycleHolds"] == [] for a in self.fake.agents.values()))
        self.assertIn("resume company Section 9: budget pause lifted", out)
        self.assertIn("resume agent Tachikoma: budget pause lifted", out)
        self.assertNotIn("left alone", out)
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(self.policy_writes(mark), [])
        self.assertNotIn("resume", out)

    def test_avatars_wait_until_the_budget_pause_is_lifted(self):
        # The VM's first sync: company paused by budget, avatars never uploaded.
        self.apply_ok()
        self.block_everything()
        self.fake.avatars.clear()
        for row in self.fake.agents.values():
            row["appearance"] = {}
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["status"], "active")
        self.assertEqual(len(self.fake.avatars), 5)
        self.assertIn("avatar Tachikoma: upload", out)

    def test_avatars_skipped_while_the_company_stays_paused(self):
        self.apply_ok()
        self.fake.company.update(status="paused", pauseReason="manual")
        out = self.apply_ok().stdout
        self.assertIn("avatars: skipped while company Section 9 is paused", out)

    def test_budget_pause_with_policy_already_right_is_nudged(self):
        self.apply_ok()
        self.block_everything()
        # Someone set "allow" in the UI, but a stale hold is still there.
        for pol in self.fake.policies.values():
            pol["unpricedUsagePolicy"] = "allow"
        tachikoma = self.agent("Tachikoma")
        tachikoma.update(status="paused", lifecycleHolds=["budget"], pauseReason="budget")
        self.fake.company.update(status="paused", pauseReason="budget")
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["status"], "active")
        self.assertEqual(tachikoma["status"], "idle")
        self.assertIn("resume agent Tachikoma: budget pause lifted", out)
        self.assertGreaterEqual(len(self.policy_writes(mark)), 1)

    def test_agent_paused_by_a_human_is_left_alone(self):
        self.apply_ok()
        saito = self.agent("Saito")
        saito.update(status="paused", lifecycleHolds=["manual"], pauseReason="manual")
        self.block_everything(skip=("Saito",))
        out = self.apply_ok().stdout
        self.assertEqual((saito["status"], saito["lifecycleHolds"], saito["pauseReason"]),
                         ("paused", ["manual"], "manual"))
        self.assertEqual(self.agent("Batou")["status"], "idle")
        self.assertEqual(self.fake.company["status"], "active")
        # The company's budget pause also held Saito; the company's is lifted, Petter's is not.
        self.assertIn("agent Saito: budget pause lifted; still paused (manual), left alone", out)
        self.assertNotIn("resume agent Saito", out)
        # A re-run with only Saito paused writes nothing but the idempotent avatar PUTs.
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(self.writes(mark), [w for w in self.writes(mark) if w["path"].endswith("/avatar")])
        self.assertEqual(saito["status"], "paused")
        self.assertIn("paused (manual): Saito: left alone", out)

    def test_budget_and_human_hold_together_keep_the_human_hold(self):
        self.apply_ok()
        saito = self.agent("Saito")
        self.fake.unpriced_events[saito["id"]] = 1
        self.fake.policies[("agent", saito["id"])]["unpricedUsagePolicy"] = "block"
        saito["lifecycleHolds"] = ["manual"]
        self.fake.reconcile()
        self.assertEqual(saito["lifecycleHolds"], ["manual", "budget"])
        out = self.apply_ok().stdout
        self.assertEqual((saito["status"], saito["lifecycleHolds"]), ("paused", ["manual"]))
        self.assertIn("agent Saito: budget pause lifted; still paused (manual), left alone", out)

    def test_company_paused_by_a_human_is_not_resumed(self):
        self.apply_ok()
        self.fake.company.update(status="paused", pauseReason="manual")
        self.fake.reconcile()
        out = self.apply_ok().stdout
        self.assertEqual((self.fake.company["status"], self.fake.company["pauseReason"]), ("paused", "manual"))
        self.assertTrue(all(a["status"] == "paused" for a in self.fake.agents.values()))
        self.assertIn("paused (manual): Section 9: left alone", out)
        self.assertIn("paused (company_paused): Batou: left alone", out)

    def test_pause_without_a_reason_is_reported_not_resumed(self):
        self.apply_ok()
        batou = self.agent("Batou")
        batou.update(status="paused", pauseReason=None)
        self.fake.expose_holds = False
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(batou["status"], "paused")
        self.assertIn("paused (budget?): Batou: resume it in Paperclip", out)
        self.assertEqual(self.policy_writes(mark), [])

    def test_pause_reason_alone_is_enough_without_holds(self):
        self.apply_ok()
        self.fake.expose_holds = False
        self.block_everything()
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["status"], "active")
        self.assertIn("resume agent Batou: budget pause lifted", out)

    def test_cap_still_exceeded_stays_paused_and_is_reported(self):
        self.apply_ok()
        batou = self.agent("Batou")
        self.fake.spent[batou["id"]] = 400
        self.fake.reconcile()
        self.assertEqual(batou["status"], "paused")
        out = self.apply_ok().stdout
        self.assertEqual(batou["status"], "paused")
        self.assertIn("still paused (budget): agent Batou", out)
        self.assertNotIn("resume agent Batou", out)

    def test_dry_run_plans_policies_and_resumes_but_writes_nothing(self):
        self.apply_ok()
        self.block_everything(skip=("Saito",))
        self.agent("Saito").update(status="paused", lifecycleHolds=["manual"], pauseReason="manual")
        before = copy.deepcopy((self.fake.company, self.fake.agents, self.fake.policies))
        mark = len(self.fake.log)
        out = self.apply_ok("--dry-run").stdout
        self.assertEqual(self.writes(mark), [])
        self.assertEqual(before, (self.fake.company, self.fake.agents, self.fake.policies))
        self.assertIn("[dry-run] policy company Section 9: unpriced block -> allow", out)
        self.assertIn("[dry-run] resume company Section 9: budget pause", out)
        self.assertIn("[dry-run] resume agent Tachikoma: budget pause", out)
        self.assertIn("[dry-run] paused (manual): Saito: left alone", out)

    def test_dry_run_on_an_empty_paperclip_plans_the_policies(self):
        out = self.apply_ok("--dry-run").stdout
        self.assertEqual(self.writes(), [])
        self.assertIn("[dry-run] policy agent Tachikoma: create 1000 cents, unpriced allow", out)

    def test_roster_unpriced_usage_must_be_allow_or_block(self):
        roster = (ROOT / "agents" / "roster.toml").read_text().replace('unpriced_usage = "allow"', 'unpriced_usage = "maybe"')
        path = Path(self.tmp.name) / "roster.toml"
        path.write_text(roster)
        proc = self.apply("--roster", str(path))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("unpriced_usage must be 'allow' or 'block'", proc.stderr)
        self.assertEqual(self.fake.log, [])

    def test_agent_not_in_roster_is_reported_not_touched(self):
        other = self.fake.seed_agent("Gally", "claude_local", {"model": "x"})
        before = copy.deepcopy(other)
        out = self.apply_ok().stdout
        self.assertIn("not in roster: Gally", out)
        self.assertEqual(other, before)
        self.assertFalse([r for r in self.fake.log if other["id"] in r["path"]])

    def test_board_approval_hires_then_approves(self):
        self.fake.company["requireBoardApprovalForNewAgents"] = True
        out = self.apply_ok().stdout
        self.assertEqual(len(self.fake.agents), 5)
        self.assertTrue(all(a["status"] == "idle" for a in self.fake.agents.values()))
        self.assertEqual(len([r for r in self.fake.log if r["path"].endswith("/agent-hires")]), 5)
        self.assertEqual(len(self.fake.avatars), 5)
        self.assertIn("agent Saito: create (hire, approve)", out)

    def test_keys_never_reach_the_output(self):
        self.apply_ok("--set-api-key")
        self.apply_ok("--dry-run")
        # A server that echoes the credentials back in an error body.
        self.fake.fail[("PATCH", r"/api/companies/.*")] = (500, f"{self.board_key} {self.hermes_key}")
        self.fake.company["budgetMonthlyCents"] = 5
        proc = self.apply()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("PATCH /api/companies/", proc.stderr)
        self.assertIn("500", proc.stderr)
        for text in (self.last.stdout, self.last.stderr):
            self.assertNotIn(self.board_key, text)
            self.assertNotIn(self.hermes_key, text)

    def test_every_request_carries_the_board_key(self):
        self.apply_ok()
        self.assertTrue(all(r["auth"] == "Bearer " + self.board_key for r in self.fake.log))

    def test_api_error_exits_nonzero_with_method_path_status(self):
        self.fake.fail[("POST", r"/api/companies/[^/]+/routines")] = (500, "boom")
        proc = self.apply()
        self.assertEqual(proc.returncode, 1)
        self.assertRegex(proc.stderr, r"POST /api/companies/\S+/routines -> 500")

    def test_missing_company_is_an_error(self):
        self.fake.company["name"] = "Other"
        proc = self.apply()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("company 'Section 9' not found", proc.stderr)
        self.assertEqual(self.writes(), [])

    def test_missing_secret_is_an_error(self):
        (Path(self.secrets_dir) / "hermes.env").write_text("OPENROUTER_API_KEY=x\n")
        proc = self.apply()
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("API_SERVER_KEY is missing", proc.stderr)
        self.assertEqual(self.fake.log, [])


if __name__ == "__main__":
    unittest.main()
