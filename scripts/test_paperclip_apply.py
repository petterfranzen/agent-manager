"""Tests for paperclip-apply.py against a small fake Paperclip.

Run: python3 -m unittest scripts/test_paperclip_apply.py
The fake implements only the endpoints the script uses, with the response
shapes of Paperclip v2026.1023.0.
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
import unittest
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "paperclip-apply.py"
REDACTED = "***REDACTED***"


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
        }
        self.companies = [self.company]
        self.agents = {}
        self.secrets = {}   # agent id -> stored api key
        self.avatars = {}   # agent id -> sha256
        self.routines = {}
        self.triggers = {}

    def seed_agent(self, name, adapter_type="hermes_gateway", config=None, **fields):
        aid = str(uuid.uuid4())
        row = {"id": aid, "companyId": self.company["id"], "name": name, "role": "general",
               "title": None, "status": "idle", "adapterType": adapter_type,
               "adapterConfig": config or {}, "budgetMonthlyCents": 0, "appearance": {}}
        row.update(fields)
        self.agents[aid] = row
        return row

    def view_agent(self, row):
        row = copy.deepcopy(row)
        if "apiKey" in row["adapterConfig"]:
            row["adapterConfig"]["apiKey"] = REDACTED
        return row

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
        return 200, self.view_agent(row)

    def h_avatar(self, body, cid, aid):
        row = self.agents.get(aid)
        if not row:
            return 404, {"error": "Agent not found"}
        if row["status"] == "pending_approval":
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

    routes = [
        ("GET", r"/api/companies", h_companies),
        ("PATCH", r"/api/companies/([^/]+)", h_patch_company),
        ("GET", r"/api/companies/([^/]+)/agents", h_agents),
        ("POST", r"/api/companies/([^/]+)/agents", h_create_agent),
        ("POST", r"/api/companies/([^/]+)/agent-hires", h_hire),
        ("POST", r"/api/agents/([^/]+)/approve", h_approve),
        ("PATCH", r"/api/agents/([^/]+)", h_patch_agent),
        ("PUT", r"/api/companies/([^/]+)/agents/([^/]+)/avatar", h_avatar),
        ("GET", r"/api/companies/([^/]+)/routines", h_routines),
        ("POST", r"/api/companies/([^/]+)/routines", h_create_routine),
        ("PATCH", r"/api/routines/([^/]+)", h_patch_routine),
        ("POST", r"/api/routines/([^/]+)/triggers", h_create_trigger),
        ("PATCH", r"/api/routine-triggers/([^/]+)", h_patch_trigger),
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

    def test_first_run_creates_everything(self):
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.company["budgetMonthlyCents"], 2000)
        self.assertEqual(sorted(a["name"] for a in self.fake.agents.values()),
                         ["Batou", "Ishikawa", "Saito", "Tachikoma", "Togusa"])
        tachikoma = self.agent("Tachikoma")
        self.assertEqual(tachikoma["role"], "engineer")
        self.assertEqual(tachikoma["title"], "Nightly builder")
        self.assertEqual(tachikoma["budgetMonthlyCents"], 1000)
        cfg = tachikoma["adapterConfig"]
        self.assertEqual(cfg["timeoutSec"], 7200)
        self.assertEqual(cfg["apiBaseUrl"], "http://127.0.0.1:8642")
        self.assertEqual(cfg["paperclipApiUrl"], "http://127.0.0.1:3100")
        self.assertEqual(cfg["sessionKeyStrategy"], "issue")
        self.assertEqual(cfg["instructions"], (ROOT / "agents/nightly-builder.md").read_text())
        self.assertEqual(self.agent("Batou")["adapterConfig"]["timeoutSec"], 600)
        self.assertEqual(self.fake.secrets[tachikoma["id"]], self.hermes_key)
        self.assertEqual(len(self.fake.avatars), 5)

        self.assertEqual(len(self.fake.routines), 3)
        self.assertTrue(all(r["status"] == "paused" for r in self.fake.routines.values()))
        by_title = {r["title"]: r for r in self.fake.routines.values()}
        saito = by_title["Morning ops report"]
        self.assertEqual(saito["assigneeAgentId"], self.agent("Saito")["id"])
        self.assertEqual(saito["description"], "Write the morning ops report, following your brief.")
        trig = [t for t in self.fake.triggers.values() if t["routineId"] == saito["id"]]
        self.assertEqual([(t["cronExpression"], t["timezone"], t["enabled"]) for t in trig],
                         [("30 6 * * *", "Europe/Stockholm", True)])
        self.assertIn("agent Tachikoma: create", out)
        self.assertIn("routine Nightly build: create (paused)", out)
        self.assertEqual(len(self.key_sends()), 5)

    def test_second_run_changes_nothing_and_sends_no_key(self):
        self.apply_ok()
        state = copy.deepcopy((self.fake.agents, self.fake.routines, self.fake.triggers, self.fake.company))
        mutations, mark = self.fake.mutations, len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(self.fake.mutations, mutations)
        self.assertEqual(state, (self.fake.agents, self.fake.routines, self.fake.triggers, self.fake.company))
        self.assertEqual(self.key_sends(mark), [])
        # The only non-GET calls are avatar uploads, which the server ignores when unchanged.
        self.assertTrue(all(r["method"] == "PUT" and r["path"].endswith("/avatar")
                            for r in self.writes(mark)))
        self.assertIn("ok: Batou unchanged", out)
        self.assertIn("ok: routine Nightly build unchanged", out)
        self.assertNotIn("create", out)
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
        routine["status"] = "active"
        mark = len(self.fake.log)
        out = self.apply_ok().stdout
        self.assertEqual(batou["budgetMonthlyCents"], 400)
        self.assertEqual(batou["adapterConfig"]["instructions"], (ROOT / "agents/coder.md").read_text())
        self.assertEqual(batou["adapterConfig"]["apiKey"], self.hermes_key)
        self.assertEqual(self.key_sends(mark), [])
        self.assertEqual(trig["cronExpression"], "0 1 * * *")
        self.assertEqual(routine["status"], "paused")
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
        self.assertIn("[dry-run] routine Nightly build: create (paused)", out)
        self.apply_ok()
        mark = len(self.fake.log)
        self.agent("Batou")["budgetMonthlyCents"] = 1
        self.apply_ok("--dry-run")
        self.assertEqual(self.writes(mark), [])
        self.assertEqual(self.agent("Batou")["budgetMonthlyCents"], 1)

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
