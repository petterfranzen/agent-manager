"""Tests for deploy/crew-status.py against stub Paperclip, OpenRouter and GitHub servers.

Run: python3 -m unittest scripts/test_crew_status.py
"""

import base64
import datetime as dt
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("crew", ROOT / "deploy" / "crew-status.py")
crew = importlib.util.module_from_spec(spec)
spec.loader.exec_module(crew)

BOARD = "pcp_board_" + "t" * 20
ORKEY = "sk-or-" + "k" * 20
GHTOK = "github_pat_" + "g" * 20
NOW = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)


def ago(hours):
    return (NOW - dt.timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


class Stub:
    def __init__(self):
        self.writes = []
        self.file = None  # (sha, decoded JSON) on the crew-status branch
        self.branch = True
        self.agents = [
            {"id": "a-tachi", "name": "Tachikoma", "status": "running"},
            {"id": "a-batou", "name": "Batou", "status": "idle"},
            {"id": "a-ishi", "name": "Ishikawa", "status": "running"},
            {"id": "a-togusa", "name": "Togusa", "status": "paused"},
            # Saito is in the roster but not in Paperclip: "off".
        ]
        self.issues = {
            "in_progress": [
                {"id": "i1", "title": "Nightly build", "status": "in_progress", "assigneeAgentId": "a-tachi",
                 "updatedAt": ago(1), "visibility": "open"},
                {"id": "i2", "title": "Errand watch", "status": "in_progress", "assigneeAgentId": "a-ishi",
                 "updatedAt": ago(1), "visibility": "open"},
            ],
            "done": [
                {"id": "i3", "identifier": "SEC-8", "title": "Crew page: command staff", "status": "done",
                 "assigneeAgentId": "a-batou", "updatedAt": ago(30), "visibility": "open"},
                {"id": "i4", "title": "Gotham errand shortlist", "status": "done",
                 "assigneeAgentId": "a-ishi", "updatedAt": ago(20), "visibility": "open"},
                {"id": "i5", "title": "Errand watch", "status": "done", "assigneeAgentId": "a-ishi",
                 "updatedAt": ago(10), "visibility": "open"},
                # Allowlisted prefix, private content: the deny terms still keep it out.
                {"id": "i6", "title": "Night errand scan in Gotham", "status": "done",
                 "assigneeAgentId": "a-ishi", "updatedAt": ago(5), "visibility": "open"},
                {"id": "i7", "title": "Check: Petter's errands", "status": "done",
                 "assigneeAgentId": "a-batou", "updatedAt": ago(4), "visibility": "open"},
                {"id": "i8", "title": "Crew page: private draft", "status": "done",
                 "assigneeAgentId": "a-batou", "updatedAt": ago(3), "visibility": "private"},
            ],
        }
        self.runs = [{"agentId": "a-tachi", "finishedAt": ago(2)}, {"agentId": "a-batou", "finishedAt": ago(6)},
                     {"agentId": "a-ishi", "startedAt": ago(1)}]
        self.costs = [{"agentId": "a-tachi", "costCents": 120.4}, {"agentId": "a-ishi", "costCents": 50}]
        self.prs = [
            {"title": "Night 2026-10-31: Koi Drift in Gleam", "merged_at": ago(26),
             "head": {"ref": "night/2026-10-31-koi-drift"}, "html_url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/9"},
            {"title": "Crew page: GITS-95 redesign + command staff (SEC-8)", "merged_at": ago(29),
             "head": {"ref": "task/crew-gits95"}, "html_url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/6"},
            {"title": "LOG: Petter's wishes, starting with pixel art", "merged_at": ago(27),
             "head": {"ref": "claude/log-wishes"}, "html_url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/10"},
            {"title": "Night 2026-11-01: Gotham metro map", "merged_at": ago(2),
             "head": {"ref": "night/2026-11-01-metro"}, "html_url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/12"},
            {"title": "Crew page v2", "merged_at": None, "head": {"ref": "task/crew-live"},
             "html_url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/11"},
        ]

    def handle(self, method, path, body, auth):
        if path == "/v1/key":
            if auth != "Bearer " + ORKEY:
                return 401, {"error": "bad key"}
            return 200, {"data": {"usage": 3.0, "usage_monthly": 1.33, "limit": 20}}
        if path.startswith("/repos/"):
            if auth != "Bearer " + GHTOK:
                return 401, {"message": "Bad credentials"}
            if path.startswith("/repos/petterfranzen/ghost-in-the-machine/pulls?"):
                return 200, self.prs
            if path.endswith("/git/ref/heads/crew-status"):
                return (200, {"object": {"sha": "b1"}}) if self.branch else (404, {"message": "Not Found"})
            if path.endswith("/git/ref/heads/main"):
                return 200, {"object": {"sha": "m1"}}
            if method == "POST" and path.endswith("/git/refs"):
                self.writes.append(("ref", body))
                self.branch = True
                return 201, {"ref": body["ref"]}
            if "/contents/crew/status.json" in path:
                if method == "GET":
                    if not self.file:
                        return 404, {"message": "Not Found"}
                    sha, doc = self.file
                    return 200, {"sha": sha, "content": base64.b64encode(json.dumps(doc).encode()).decode()}
                if method == "PUT":
                    self.writes.append(("put", body))
                    self.file = ("s%d" % len(self.writes), json.loads(base64.b64decode(body["content"])))
                    return 200, {"content": {"sha": self.file[0]}}
            return 404, {"message": "no route"}
        if auth != "Bearer " + BOARD:
            return 401, {"error": "Board access required"}
        if method != "GET":
            return 405, {"error": "read only"}
        if path == "/api/companies":
            return 200, [{"id": "c1", "name": "Section 9", "status": "active"}]
        if path.endswith("/agents"):
            return 200, self.agents
        if "/issues?status=" in path:
            return 200, self.issues.get(path.split("status=")[1], [])
        if "/heartbeat-runs" in path:
            return 200, self.runs
        if "/costs/by-agent?" in path:
            return 200, self.costs
        return 404, {"error": "no route"}


def serve(stub):
    class H(BaseHTTPRequestHandler):
        def any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            code, out = stub.handle(self.command, self.path, body, self.headers.get("Authorization"))
            data = json.dumps(out).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        do_GET = do_POST = do_PUT = do_PATCH = any

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class CrewStatusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "paperclip-board.env").write_text(f"PAPERCLIP_BOARD_KEY={BOARD}\n")
        (d / "hermes.env").write_text(f"API_SERVER_KEY=x\nOPENROUTER_API_KEY=\"{ORKEY}\"\n")
        (d / "sandbox.env").write_text(f"GH_TOKEN={GHTOK}\n")
        (d / "crew-deny.txt").write_text("# test terms\nerrand\ngotham\n")
        self.stub = Stub()
        server = serve(self.stub)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        self.args = ["--secrets-dir", str(d), "--roster", str(ROOT / "agents" / "roster.toml"),
                     "--paperclip", url, "--openrouter", url + "/v1", "--github", url]

    def run_ok(self, *extra):
        self.assertEqual(crew.main(self.args + list(extra)), 0)

    def puts(self):
        return [b for kind, b in self.stub.writes if kind == "put"]

    def published(self):
        self.run_ok()
        return self.stub.file[1]

    def agent(self, doc, name):
        return next(a for a in doc["agents"] if a["name"] == name)

    def test_private_titles_never_appear(self):
        doc = self.published()
        text = json.dumps(doc, ensure_ascii=False)
        for word in ("Errand", "errand", "Gotham", "Petter", "private draft",
                     "pixel art"):
            self.assertNotIn(word, text)
        ishi = self.agent(doc, "Ishikawa")
        self.assertEqual(ishi["now"], "Research (private)")
        self.assertTrue(all(r["text"] == "Research (private)" and "url" not in r for r in ishi["recent"]))
        # One private line per run of them, not one per private issue.
        self.assertEqual(len(ishi["recent"]), 1)
        self.assertEqual(self.agent(doc, "Batou")["recent"][0]["text"], "On a private task")

    def test_missing_deny_file_refuses_to_publish(self):
        (Path(self.tmp.name) / "crew-deny.txt").unlink()
        self.assertEqual(crew.main(self.args), 1)
        self.assertEqual(self.stub.writes, [])

    def test_deny_terms_stop_the_publish(self):
        # A private word in an allowlisted field (here: a roster title) fails the run, writes nothing.
        roster = Path(self.tmp.name) / "roster.toml"
        roster.write_text((ROOT / "agents" / "roster.toml").read_text().replace(
            'title = "Researcher"', 'title = "Gotham errands"'))
        args = list(self.args)
        args[args.index("--roster") + 1] = str(roster)
        self.assertEqual(crew.main(args), 1)
        self.assertEqual(self.stub.writes, [])

    def test_public_work_and_schema(self):
        doc = self.published()
        crew.validate(doc)
        self.assertEqual(doc["budget"], {"usedPercent": 7, "month": NOW.strftime("%Y-%m")})  # 133 / 1900
        tachi = self.agent(doc, "Tachikoma")
        self.assertEqual((tachi["role"], tachi["state"], tachi["now"]), ("Nightly builder", "working", "Nightly build"))
        self.assertEqual(tachi["budgetPercent"], 12)  # 120.4 / 1000
        self.assertEqual(tachi["recent"], [{"at": crew.iso(crew.ts(ago(26))),
                                            "text": "Night 2026-10-31: Koi Drift in Gleam",
                                            "url": crew.PAGES + "2026-10-31-koi-drift/"}])
        self.assertEqual(tachi["lastActiveAt"], crew.iso(crew.ts(ago(2))))
        batou = self.agent(doc, "Batou")
        self.assertEqual((batou["state"], batou["now"], batou["budgetPercent"]), ("idle", None, 0))
        texts = [r["text"] for r in batou["recent"]]
        self.assertIn("Crew page: command staff", texts)
        pr = next(r for r in batou["recent"] if r["text"].startswith("Crew page: GITS-95"))
        self.assertEqual(pr["url"], "https://github.com/petterfranzen/ghost-in-the-machine/pull/6")
        self.assertEqual(self.agent(doc, "Togusa")["state"], "paused")
        saito = self.agent(doc, "Saito")
        self.assertEqual((saito["state"], saito["lastActiveAt"], saito["recent"]), ("off", None, []))

    def test_no_money_or_secrets_leave(self):
        self.run_ok()
        sent = json.dumps(self.stub.writes)
        for s in (BOARD, ORKEY, GHTOK, "$", "costCents"):
            self.assertNotIn(s, sent)
        put = self.puts()[0]
        self.assertEqual((put["branch"], put["author"]["email"]), ("crew-status", "agents@sectionnine.dev"))

    def test_creates_the_branch_once(self):
        self.stub.branch = False
        self.run_ok()
        self.assertEqual(self.stub.writes[0], ("ref", {"ref": "refs/heads/crew-status", "sha": "m1"}))
        self.assertEqual(len(self.puts()), 1)
        self.assertNotIn("sha", self.puts()[0])

    def test_skips_when_only_the_time_changed_within_3_hours(self):
        self.run_ok()
        sha, doc = self.stub.file
        doc["generatedAt"] = crew.iso(NOW - dt.timedelta(hours=2))
        self.run_ok()
        self.assertEqual(len(self.puts()), 1)
        # Older than 3 hours: write again, so the page can tell the feed is alive.
        doc["generatedAt"] = crew.iso(NOW - dt.timedelta(hours=4))
        self.run_ok()
        self.assertEqual(len(self.puts()), 2)
        self.assertEqual(self.puts()[1]["sha"], sha)
        # A real change writes straight away.
        self.stub.agents[1]["status"] = "running"
        self.run_ok()
        self.assertEqual(len(self.puts()), 3)

    def test_dry_run_writes_nothing(self):
        self.run_ok("--dry-run")
        self.assertEqual(self.stub.writes, [])

    def test_refuses_non_loopback_paperclip_and_foreign_github(self):
        for flag, value in (("--paperclip", "http://example.com:3100"), ("--github", "https://example.com")):
            args = list(self.args)
            args[args.index(flag) + 1] = value
            self.assertEqual(crew.main(args), 1)
        self.assertEqual(self.stub.writes, [])


if __name__ == "__main__":
    unittest.main()
