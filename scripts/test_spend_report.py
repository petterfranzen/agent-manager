"""Tests for deploy/spend-report.py against stub OpenRouter and Paperclip servers.

Run: python3 -m unittest scripts/test_spend_report.py
"""

import datetime as dt
import importlib.util
import json
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("spend", ROOT / "deploy" / "spend-report.py")
spend = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spend)

BOARD = "pcp_board_" + "t" * 20
ORKEY = "sk-or-" + "k" * 20


class Stub:
    def __init__(self):
        self.usage = 1.00
        self.posts = []
        self.runs = []
        now = dt.datetime.now(dt.timezone.utc)
        for agent, hours, i, o in [("a-tachi", 5, 8_000_000, 90_000), ("a-ishi", 3, 2_000_000, 20_000)]:
            self.runs.append({"agentId": agent, "inputTokens": str(i), "outputTokens": str(o),
                              "finishedAt": (now - dt.timedelta(hours=hours)).isoformat()})

    def handle(self, method, path, body, auth):
        if path == "/v1/key":
            if auth != "Bearer " + ORKEY:
                return 401, {"error": "bad key"}
            return 200, {"data": {"usage": self.usage, "usage_daily": 0.2, "usage_monthly": self.usage,
                                  "limit": 20, "limit_remaining": 20 - self.usage}}
        if auth != "Bearer " + BOARD:
            return 401, {"error": "Board access required"}
        if method == "POST":
            self.posts.append((path, body))
            return 201, {"id": "issue-1"} if path.endswith("/issues") else {"ok": True}
        if path == "/api/companies":
            return 200, [{"id": "c1", "name": "Section 9", "status": "active"}]
        if path.endswith("/agents"):
            return 200, [{"id": "a-tachi", "name": "Tachikoma"}, {"id": "a-ishi", "name": "Ishikawa"}]
        if "/heartbeat-runs" in path:
            return 200, self.runs
        if "/issues?status=" in path:
            return 200, [{"id": "issue-1", "title": "Spend"}]
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

        do_GET = do_POST = any

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


class SpendTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        (d / "paperclip-board.env").write_text(f"PAPERCLIP_BOARD_KEY={BOARD}\n")
        (d / "hermes.env").write_text(f"API_SERVER_KEY=x\nOPENROUTER_API_KEY=\"{ORKEY}\"\n")
        self.state = d / "state.json"
        self.stub = Stub()
        server = serve(self.stub)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_address[1]}"
        self.args = ["--secrets-dir", str(d), "--state", str(self.state),
                     "--paperclip", url, "--openrouter", url + "/v1"]

    def run_ok(self, *extra):
        self.assertEqual(spend.main(self.args + list(extra)), 0)

    def events(self):
        return [b for p, b in self.stub.posts if p.endswith("/cost-events")]

    def comments(self):
        return [b["body"] for p, b in self.stub.posts if p.endswith("/comments")]

    def test_split_weights_and_sums_exactly(self):
        out = spend.split(Decimal("1.00"), {"a": 1, "b": 3, "c": 0})
        self.assertEqual(set(out), {"a", "b"})
        self.assertEqual(sum(out.values()), Decimal("1.00"))
        self.assertEqual(out["a"], Decimal("0.250000"))

    def test_first_run_books_nothing_and_records_state(self):
        self.run_ok()
        self.assertEqual(self.events(), [])
        self.assertIn("First report", self.comments()[0])
        self.assertEqual(json.loads(self.state.read_text())["usage"], "1.0")

    def test_second_run_books_the_delta_per_agent(self):
        self.state.write_text(json.dumps({"usage": "0.60", "at": (dt.datetime.now(dt.timezone.utc)
                                                                  - dt.timedelta(days=1)).isoformat()}))
        self.run_ok()
        ev = {e["agentId"]: e for e in self.events()}
        self.assertEqual(set(ev), {"a-tachi", "a-ishi"})
        total = sum(Decimal(e["costCents"]) for e in ev.values())
        self.assertEqual(total, Decimal("40.0"))  # $0.40 in cents
        self.assertGreater(Decimal(ev["a-tachi"]["costCents"]), Decimal(ev["a-ishi"]["costCents"]))
        self.assertEqual(ev["a-tachi"]["idempotencyKey"][:11], "openrouter-")
        body = self.comments()[0]
        self.assertIn("Tachikoma: $", body)
        self.assertNotIn(ORKEY, body + json.dumps(self.stub.posts))

    def test_spend_without_runs_carries_over(self):
        before = {"usage": "0.60", "at": dt.datetime.now(dt.timezone.utc).isoformat()}
        self.state.write_text(json.dumps(before))
        self.run_ok()
        self.assertEqual(self.events(), [])
        self.assertIn("carries over", self.comments()[0])
        self.assertEqual(json.loads(self.state.read_text()), before)

    def test_dry_run_writes_nothing(self):
        self.run_ok("--dry-run")
        self.assertEqual(self.stub.posts, [])
        self.assertFalse(self.state.exists())

    def test_refuses_a_non_loopback_paperclip(self):
        args = list(self.args)
        args[args.index("--paperclip") + 1] = "http://example.com:3100"
        self.assertEqual(spend.main(args), 1)
        self.assertEqual(self.stub.posts, [])


if __name__ == "__main__":
    unittest.main()
