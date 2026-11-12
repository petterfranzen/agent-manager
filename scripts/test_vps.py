"""Tests for vps/dispatch.py, sandbox/s9-task and scripts/health-run.sh against stub GitHub,
Hermes and OpenRouter.

Run: python3 -m unittest scripts/test_vps.py
"""

import base64
import contextlib
import datetime as dt
import fcntl
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "vps" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatch = load("dispatch", "dispatch.py")
hq = load("hq_status", "hq-status.py")
sca = load("sca_watch", "sca-watch.py")

GH = "github_pat_" + "g" * 20
ORKEY = "sk-or-" + "k" * 20
HKEY = "hermes-" + "h" * 20
PREFIX = "/gh/repos/petterfranzen/section9-hq"


class Stub:
    def __init__(self):
        self.issues, self.comments, self.hermes_calls = {}, [], []
        self.remaining = 15.0
        self.monthly, self.daily, self.bump = 5.0, 0.25, 0.0
        self.key = None             # None: the normal /key reply; else the reply body to send
        self.key_calls, self.key_fail_after = 0, None   # key_fail_after: fail /key reads after this many
        self.page_size, self.link = None, None     # page_size: serve issue lists in pages of this size
        self.hermes = "report"      # report | silent | slow | error
        self.hermes_model, self.hermes_runtime = None, None   # what the reply echoes / served
        self.next = 1
        self.gh_comments, self.pinned, self.crew_auth = [], None, "unset"
        self.crew_sha, self.checks = "tree1", []
        self.hq_sha, self.hq = "hq1", {"ghost.yaml": "name: ghost\nlabel: Batou's\n", "notes.md": "x", "sub/deep.yaml": "name: no\n"}
        self.crew = {"index.html": "<h1>crew</h1>", "status.json": "SAMPLE", "css/app.css": "a{}"}
        self.osv_batch, self.osv_vulns, self.osv_calls = {"results": []}, {}, []
        self.kev = {"catalogVersion": "test", "vulnerabilities": []}
        self.osv_auth = "unset"

    def add(self, labels, title="Do a thing", state="open", age_min=None):
        n = self.next
        self.next += 1
        updated = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min or 0)   # without age_min: a fixed stamp, or "now" for status:working
        self.issues[n] = {"number": n, "title": title, "body": f"body of {n}", "state": state,
                          "created_at": updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "updated_at": "2026-11-01T10:00:00Z" if age_min is None and "status:working" not in labels else updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "html_url": f"https://github.com/petterfranzen/section9-hq/issues/{n}",
                          "labels": [{"name": x} for x in labels]}
        return n

    def todo_count(self):
        return sum("status:todo" in self.labels(n) for n in self.issues)

    def labels(self, n):
        return {x["name"] for x in self.issues[n]["labels"]}

    def gh(self, method, path, query, body, auth):
        if auth != "Bearer " + GH:
            return 401, {"message": "Bad credentials"}
        if path == "/gh/graphql":
            self.pinned = body["variables"]["id"]
            return 200, {"data": {}}
        rest = path[len(PREFIX):]
        if method == "GET" and rest == "/issues/comments":
            return 200, self.gh_comments
        if method == "GET" and rest == "/issues":
            want = query.get("labels", [""])[0]
            st = query.get("state", ["open"])[0]
            found = [i for i in self.issues.values() if st in (i["state"], "all")
                     and (not want or want in self.labels(i["number"]))]
            size = min(int(query.get("per_page", ["30"])[0]), self.page_size or 10**6)
            page = int(query.get("page", ["1"])[0])
            if page * size < len(found):      # GitHub's Link header, absolute URL
                q = {k: v[0] for k, v in query.items()}
                q.update(page=page + 1, per_page=size)
                self.link = f'<{self.base}{path}?{urlencode(q)}>; rel="next"'
            return 200, found[(page - 1) * size:page * size]
        if method == "POST" and rest == "/issues":
            n = self.add(body.get("labels", []), body["title"])
            self.issues[n]["body"] = body["body"]
            self.issues[n]["node_id"] = f"node-{n}"
            return 201, self.issues[n]
        parts = rest.strip("/").split("/")
        if parts[0] == "issues" and len(parts) >= 2 and int(parts[1]) in self.issues:
            issue = self.issues[int(parts[1])]
            if len(parts) == 3 and method == "POST":
                self.comments.append((issue["number"], body["body"]))
                return 201, {"id": 1}
            if method == "PATCH":
                if "labels" in body:
                    issue["labels"] = [{"name": x} for x in body["labels"]]
                issue["state"] = body.get("state", issue["state"])
                issue["body"] = body.get("body", issue["body"])
                # GitHub moves updated_at on every change; the dispatcher's stale sweep reads it.
                issue["updated_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                return 200, issue
            return 200, issue
        return 404, {"message": "Not Found", "echo": auth}

    def handle(self, method, path, query, body, auth, headers):
        if path.startswith("/gh/repos/petterfranzen/ghost-in-the-machine/git/"):
            self.crew_auth = auth
            if path.endswith("/trees/main%3Ahq"):
                if self.hq is None:
                    return 404, {"message": "Not Found"}
                return 200, {"sha": self.hq_sha, "truncated": False, "tree": [
                    {"path": n, "type": "blob", "sha": "sha-hq-" + n.replace("/", "-"), "size": len(c)} for n, c in self.hq.items()]}
            if "/blobs/sha-hq-" in path:
                name = path.rsplit("/sha-hq-", 1)[1]
                return 200, {"content": base64.b64encode(next(c for n, c in self.hq.items() if n.replace("/", "-") == name).encode()).decode()}
            if path.endswith("/trees/main%3Acrew"):
                return 200, {"sha": self.crew_sha, "truncated": False, "tree": [
                    {"path": n, "type": "blob", "sha": "sha-" + n.replace("/", "-"), "size": len(c)} for n, c in self.crew.items()]}
            name = path.rsplit("/sha-", 1)[1]
            c = next(c for n, c in self.crew.items() if n.replace("/", "-") == name)
            return 200, {"content": base64.b64encode(c.encode()).decode()}
        if path.startswith("/ci/"):
            if path.endswith("/check-runs"):
                return 200, {"total_count": len(self.checks), "check_runs": self.checks}
            return self.gh(method, "/gh" + path[len("/ci"):], query, body, auth)
        if path.startswith("/gh/"):
            return self.gh(method, path, query, body, auth)
        if path == "/or/key":
            self.key_calls += 1
            if self.key_fail_after is not None and self.key_calls > self.key_fail_after:
                return 500, {"error": "key read refused"}
            if self.key is not None:
                return 200, self.key
            return 200, {"data": {"usage": self.monthly, "limit": 20, "limit_remaining": self.remaining,
                                  "usage_monthly": self.monthly, "usage_daily": self.daily}}
        if path == "/osv/v1/querybatch":
            self.osv_auth = auth
            self.osv_calls.append(body)
            return 200, self.osv_batch
        if path.startswith("/osv/v1/vulns/"):
            vid = path.rsplit("/", 1)[1]
            return (200, self.osv_vulns[vid]) if vid in self.osv_vulns else (404, {"message": "no such advisory"})
        if path == "/kev/kev.json":
            return 200, self.kev
        if path == "/hermes/v1/models":
            return 200, {"data": []}
        if path == "/hermes/v1/chat/completions":
            self.hermes_calls.append((body, auth, headers.get("X-Hermes-Session-Id")))
            self.monthly += self.bump          # a run's cost shows up in the counter it returns
            n = int(body["messages"][1]["content"].split("#")[1].split(":")[0])
            if self.hermes == "error":
                return 500, {"error": f"upstream said {ORKEY}"}
            if self.hermes in ("slow", "slow-report"):
                time.sleep(2.5)
            if self.hermes in ("report", "slow-report"):   # what the agent does with s9-task
                self.issues[n]["labels"] = [{"name": x} for x in self.labels(n) - {"status:working"}] + [{"name": "status:review"}]
            reply = {"model": self.hermes_model or body.get("model", "hermes-agent"),
                     "choices": [{"message": {"content": "line 1\nline 2\nall done"}}],
                     "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
            if self.hermes_runtime:
                reply["runtime"] = self.hermes_runtime
            return 200, reply
        return 404, {}


def serve(stub):
    class H(BaseHTTPRequestHandler):
        def any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            u = urlsplit(self.path)
            stub.link = None
            if u.path == "/hermes/v1/chat/completions" and body and body.get("stream"):
                self.sse_chat(body, u)
                return
            code, out = stub.handle(self.command, u.path, parse_qs(u.query), body, self.headers.get("Authorization"), self.headers)
            raw = json.dumps(out).encode()
            self.send_response(code)
            if stub.link:
                self.send_header("Link", stub.link)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def sse_chat(self, body, u):
            """Serve a streaming chat completion: an early partial chunk when the stub is slow,
            then the whole reply as chat.completion.chunk frames (so the dispatcher can keep the
            text it saw before its timeout)."""
            auth, headers = self.headers.get("Authorization"), self.headers
            slow = stub.hermes in ("slow", "slow-report")

            def frame(payload):
                return ("data: " + json.dumps(payload) + "\n\n").encode()

            if slow:      # the run has produced something, then stalls past the dispatcher's timeout
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(frame({"model": "hermes-agent",
                                        "choices": [{"delta": {"content": "partial line 1\n"}}]}))
                self.wfile.flush()
            code, out = stub.handle(self.command, u.path, parse_qs(u.query), body, auth, headers)
            if code != 200:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                raw = json.dumps(out).encode()
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                return
            if not slow:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
            content = (((out.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
            try:
                if content:
                    self.wfile.write(frame({"model": out.get("model", "hermes-agent"),
                                            "choices": [{"delta": {"content": content}}]}))
                if out.get("runtime"):
                    self.wfile.write(frame({"choices": [{"delta": {}}], "runtime": out["runtime"]}))
                self.wfile.write(frame({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                        "usage": out.get("usage", {})}))
                self.wfile.write(b"data: [DONE]\n\n")
            except BrokenPipeError:      # the dispatcher already gave up on the run
                pass

        do_GET = do_POST = do_PATCH = any

        def log_message(self, *a):
            pass
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


ROSTER = """
[[agents]]
name = "Batou"
brief = "agents/coder.md"
timeout_sec = {timeout}

[[agents]]
name = "Ishikawa"
brief = "agents/researcher.md"
timeout_sec = 60

[[agents]]
name = "Saito"
brief = "agents/ops.md"
timeout_sec = 60
[agents.routine]
title = "Morning report"
description = "Write it."
cron = "*/5 * * * *"
timezone = "Europe/Stockholm"
active = true

[[agents]]
name = "Togusa"
brief = "agents/backup-coordinator.md"
timeout_sec = 60
[agents.routine]
title = "Sweep"
description = "Sweep."
cron = "*/5 * * * *"
timezone = "Europe/Stockholm"
active = false
"""


# A roster that names a per-agent model (#101).
ROSTER_MODEL = """
[[agents]]
name = "Batou"
brief = "agents/coder.md"
timeout_sec = 60

[[agents]]
name = "Saito"
brief = "agents/ops.md"
model = "anthropic/claude-sonnet-5.5"
timeout_sec = 60
"""


class Base(unittest.TestCase):
    timeout = 60

    def setUp(self):
        self.stub = Stub()
        self.srv = serve(self.stub)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        self.stub.base = self.url
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        (t / "secrets").mkdir()
        (t / "secrets" / "hermes.env").write_text(f"OPENROUTER_API_KEY={ORKEY}\nAPI_SERVER_KEY='{HKEY}'\n")
        (t / "secrets" / "sandbox.env").write_text(f"GH_TOKEN={GH}\n")
        (t / "roster.toml").write_text(ROSTER.format(timeout=self.timeout))
        self.t = t

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def run_dispatch(self, cmd="run", limits=None, lane=None, private=None):
        argv = [cmd, "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--roster", str(self.t / "roster.toml"), "--private-routines", private or str(self.t / "none.toml"),
                "--limits", limits or str(self.t / "none-limits.toml"),
                "--github", self.url + "/gh", "--openrouter", self.url + "/or", "--hermes", self.url + "/hermes"]
        if lane is not None:
            argv += ["--lane", str(lane)]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = dispatch.main(argv)
        for text in (out.getvalue(), err.getvalue()):
            for secret in (GH, ORKEY, HKEY):
                self.assertNotIn(secret, text)
        return code, out.getvalue()


class TestDispatch(Base):
    def test_runs_only_the_oldest_todo_and_the_agent_reports(self):
        first = self.stub.add(["agent:batou", "status:todo"], "Fix the thing")
        second = self.stub.add(["agent:ishikawa", "status:todo"])
        self.stub.add(["agent:batou", "status:working"])
        self.stub.add(["status:todo"])      # nobody's
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        body, auth, session = self.stub.hermes_calls[0]
        self.assertEqual(auth, "Bearer " + HKEY)
        self.assertEqual(session, f"issue-{first}")
        self.assertIn("You are **Batou**", body["messages"][0]["content"])
        user = body["messages"][1]["content"]
        self.assertIn("Fix the thing", user)
        self.assertIn(f"issues/{first}", user)
        self.assertIn(f"s9-task --as Batou status {first}", user)
        self.assertEqual(self.stub.labels(first), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(second), {"agent:ishikawa", "status:todo"})
        self.assertEqual(self.stub.comments, [])

    def test_a_picked_issue_that_is_no_longer_todo_is_not_claimed(self):
        # #78 should-fix 3: the todo list read may be stale; re-check the issue right before claiming.
        n = self.stub.add(["agent:batou", "status:todo"])
        def steal(todo, *args, **kwargs):
            self.stub.issues[n]["labels"] = [{"name": "agent:batou"}, {"name": "status:working"}]
            return todo
        with mock.patch.object(dispatch, "hold_low_priority", side_effect=steal):
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:working"})
        self.assertIn("no longer todo", out)

    def test_no_final_status_blocks_with_last_lines(self):
        self.stub.hermes = "silent"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("without a final status", self.stub.comments[0][1])
        self.assertIn("all done", self.stub.comments[0][1])

    def test_hermes_error_blocks_and_keeps_secrets_out(self):
        self.stub.hermes = "error"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("500", self.stub.comments[0][1])
        self.assertNotIn(ORKEY, self.stub.comments[0][1])

    def test_over_the_cap_blocks_without_calling_hermes(self):
        self.stub.remaining = 0.5
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("cap", self.stub.comments[0][1])

    def test_lock_held_means_no_run(self):
        self.stub.add(["agent:batou", "status:todo"])
        (self.t / "state").mkdir()
        with open(self.t / "state" / "dispatch.lane0.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("in progress in lane 0", out)

    def test_status_reports_queue_and_hermes(self):
        self.stub.add(["agent:batou", "status:todo"])
        _, out = self.run_dispatch("status")
        self.assertIn("1 todo", out)
        self.assertIn("hermes: reachable", out)
        self.assertIn("lanes: 1", out)

    def test_refuses_plain_http_to_a_remote_host(self):
        argv = ["--secrets-dir", str(self.t / "secrets"), "--github", "http://example.com"]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(dispatch.main(argv), 1)
        self.assertIn("refusing", err.getvalue())


class TestFailClosed(Base):
    def blocked(self, reason):
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn(reason, self.stub.comments[0][1])

    def test_a_key_without_a_cap_blocks(self):
        self.stub.key = {"data": {"usage": 5, "limit": None, "limit_remaining": None, "usage_monthly": 5}}
        self.blocked("no monthly cap")

    def test_an_empty_or_unexpected_key_reply_blocks(self):
        for reply in ({}, {"data": {}}, {"data": None}, {"error": "incident"}, [], {"data": "oops"}):
            with self.subTest(reply=reply):
                self.setUp()
                self.stub.key = reply
                self.blocked("empty or not what was expected")
                self.tearDown()

    def test_a_cap_with_neither_a_remaining_balance_nor_usage_blocks(self):
        self.stub.key = {"data": {"limit": 20, "usage_monthly": 5}}     # a cap, but no usage and no limit_remaining
        self.blocked("neither a remaining balance nor usage")

    def test_a_cap_that_is_not_a_number_blocks(self):
        self.stub.key = {"data": {"limit": "lots", "limit_remaining": None}}
        self.blocked("not a number")

    def test_status_says_why(self):
        self.stub.key = {"data": {"limit": None}}
        _, out = self.run_dispatch("status")
        self.assertIn("spend: The OpenRouter key has no monthly cap", out)


class TestRecovery(Base):
    timeout = 3600          # Batou: swept after 70 min

    def test_a_killed_run_is_blocked_with_a_comment_before_work_is_picked(self):
        stuck = self.stub.add(["agent:batou", "status:working"], age_min=71)
        slow = self.stub.add(["agent:ishikawa", "status:working"], age_min=11)      # its timeout is 60 s: 11 min is stale
        fresh = self.stub.add(["agent:batou", "status:working"], age_min=69)
        todo = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(stuck), {"agent:batou", "status:blocked"})
        self.assertEqual(self.stub.labels(slow), {"agent:ishikawa", "status:blocked"})
        self.assertEqual(self.stub.labels(fresh), {"agent:batou", "status:working"})
        self.assertEqual([n for n, _ in self.stub.comments], [stuck, slow])
        self.assertIn("probably killed", self.stub.comments[0][1])
        self.assertIn("71 min", self.stub.comments[0][1])
        self.assertEqual(self.stub.labels(todo), {"agent:batou", "status:review"})     # and the queue still ran

    def test_the_sweep_uses_the_root_only_ceiling_not_the_roster_value(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("[timeout_sec]\nBatou = 1800\n")      # the roster says 3600 s
        stuck = self.stub.add(["agent:batou", "status:working"], age_min=41)    # past 1800+600 s, under 3600+600
        self.run_dispatch("run", limits=limits)
        self.assertEqual(self.stub.labels(stuck), {"agent:batou", "status:blocked"})
        self.assertIn("41 min", self.stub.comments[0][1])

    def test_with_nothing_todo_it_still_sweeps(self):
        n = self.stub.add(["agent:saito", "status:working"], age_min=12)
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:blocked"})

    def test_an_issue_without_a_known_agent_gets_the_default_timeout(self):
        young, old = self.stub.add(["status:working"], age_min=69), self.stub.add(["status:working"], age_min=71)
        self.run_dispatch()
        self.assertEqual((self.stub.labels(young), self.stub.labels(old)), ({"status:working"}, {"status:blocked"}))

    def test_a_blocked_issue_is_not_swept_twice(self):
        self.stub.add(["agent:saito", "status:working"], age_min=12)
        self.run_dispatch()
        self.run_dispatch()
        self.assertEqual(len(self.stub.comments), 1)


class TestPaging(Base):
    def test_todo_follows_link_next_to_the_oldest_issue_on_a_later_page(self):
        self.stub.page_size = 2
        for _ in range(5):
            self.stub.add(["status:todo"])                       # nobody's: the dispatcher must read past them
        last = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(last), {"agent:batou", "status:review"})

    def test_status_counts_every_page(self):
        self.stub.page_size = 3
        for _ in range(7):
            self.stub.add(["agent:batou", "status:todo"])
        _, out = self.run_dispatch("status")
        self.assertIn("7 todo", out)

    def test_routines_and_spend_read_every_page(self):
        self.stub.page_size = 1
        for _ in range(3):
            self.stub.add([], title="other")
        self.run_dispatch("spend")
        self.run_dispatch("spend")
        self.assertEqual(sum(i["title"] == "Spend" for i in self.stub.issues.values()), 1)

    def test_refuses_to_run_when_there_are_too_many_pages(self):
        self.stub.page_size = 1
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatch, "MAX_PAGES", 1):
            code, _ = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls, self.stub.comments), (1, [], []))

    def test_never_follows_a_next_link_to_another_host(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.page_size = 1
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.base = "http://127.0.0.2:1"            # the Link header points elsewhere
        code, _ = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (1, []))


class TestSpend(Base):
    def test_creates_then_updates_one_pinned_issue_without_agent_labels(self):
        self.run_dispatch("spend")
        (n, issue), = self.stub.issues.items()
        self.assertEqual((issue["title"], self.stub.labels(n), self.stub.pinned), ("Spend", set(), f"node-{n}"))
        for line in ("This month (UTC): $5.00", "Cap: $20.00", "Left: $15.00", "Today (UTC): $0.25", "As of "):
            self.assertIn(line, issue["body"])
        self.stub.remaining = 9.5
        self.run_dispatch("spend")
        self.assertEqual(len(self.stub.issues), 1)
        self.assertIn("Left: $9.50", issue["body"])
        self.assertEqual(self.stub.todo_count(), 0)

    def test_the_cash_multiplier_is_shown_alongside_usage(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("cash_multiplier = 1.32\n")
        self.run_dispatch("spend", limits=limits)
        (n, issue), = self.stub.issues.items()
        self.assertIn("This month (UTC): $5.00", issue["body"])
        self.assertIn("$6.60", issue["body"])           # cash = 5.00 x 1.32
        self.assertIn("Cash (x1.32", issue["body"])

    def test_dispatcher_ignores_it(self):
        self.run_dispatch("spend")
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])


class TestCredits(Base):
    def test_one_issue_with_steal_the_ceiling_and_the_caps_no_agent_labels(self):
        with mock.patch.object(dispatch, "SAMPLE_SECONDS", 0.01), \
                mock.patch.object(dispatch, "PROBE_SECONDS", 0.01):
            self.run_dispatch("cpu")
            self.run_dispatch("cpu")
        (n, issue), = self.stub.issues.items()
        self.assertEqual((issue["title"], self.stub.labels(n)), ("CPU credits", set()))
        for line in ("Limit:", "Steal:", "Busy:", "Caps in force:", "As of "):
            self.assertIn(line, issue["body"])
        self.assertEqual(self.stub.todo_count(), 0)      # a snapshot, not a task


class TestTimeout(Base):
    timeout = 1

    def test_timeout_keeps_the_partial_output(self):
        self.stub.hermes = "slow"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        comment = self.stub.comments[0][1]
        self.assertIn("timed out at 1 s", comment)
        self.assertIn("partial line 1", comment)          # what the run wrote before its time ran out
        self.assertIn("Set `status:todo` to try again", comment)



class TestRoutines(Base):
    def state(self, minutes_ago):
        (self.t / "state").mkdir(exist_ok=True)
        at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=minutes_ago)
        (self.t / "state" / "routines.json").write_text(json.dumps({"at": at.isoformat()}))

    def test_opens_active_routine_once(self):
        self.state(10)
        self.run_dispatch("routines")
        self.assertEqual([(i["title"], self.stub.labels(i["number"])) for i in self.stub.issues.values()],
                         [("Morning report", {"agent:saito", "status:todo"})])
        self.run_dispatch("routines")           # same window: nothing new
        self.state(10)
        self.run_dispatch("routines")           # still open: skipped
        self.assertEqual(len(self.stub.issues), 1)

    def test_a_routine_issue_in_review_does_not_block_tonights_run(self):
        # The nightly build's old issue stays open while its PR is in QA; that must not skip the next night.
        old = self.stub.add(["agent:saito", "status:review"], title="Morning report")
        blocked = self.stub.add(["agent:saito", "status:blocked"], title="Morning report")
        self.state(10)
        self.run_dispatch("routines")
        fresh = [n for n in self.stub.issues if n not in (old, blocked)]
        self.assertEqual(len(fresh), 1)
        self.assertEqual(self.stub.labels(fresh[0]), {"agent:saito", "status:todo"})

    def test_a_routine_still_todo_or_working_blocks_a_duplicate(self):
        for state in ("status:todo", "status:working"):
            self.stub.issues.clear()
            self.stub.add(["agent:saito", state], title="Morning report")
            self.state(10)
            self.run_dispatch("routines")
            self.assertEqual(len(self.stub.issues), 1, state)

    def test_a_malformed_private_routines_file_is_logged_and_the_public_routines_still_run(self):
        self.state(10)
        priv = self.t / "private.toml"
        priv.write_text('[[routines]]\nagent = "Saito"\ntitle = "SECRET-TITLE-9"\ncron = = oops\n')
        code, out = self.run_dispatch("routines", private=str(priv))
        self.assertEqual(code, 0)
        self.assertIn("private routines not loaded", out)
        self.assertNotIn("SECRET-TITLE-9", out)
        self.assertEqual([i["title"] for i in self.stub.issues.values()], ["Morning report"])

    def test_a_private_routine_missing_a_field_is_skipped_and_the_rest_run(self):
        self.state(10)
        priv = self.t / "private.toml"
        priv.write_text('[[routines]]\nagent = "Saito"\ntitle = "SECRET-TITLE-9"\n'
                        '[[routines]]\nagent = "Saito"\ntitle = "Private ok"\ncron = "* * * * *"\n')
        code, out = self.run_dispatch("routines", private=str(priv))
        self.assertEqual(code, 0)
        self.assertIn("private routine 1 skipped (check: cron)", out)
        self.assertNotIn("SECRET-TITLE-9", out)
        self.assertEqual(sorted(i["title"] for i in self.stub.issues.values()), ["Morning report", "Private ok"])

    def test_a_routine_can_claim_a_priority(self):
        roster = (self.t / "roster.toml")
        text = roster.read_text() if roster.exists() else None
        if text is None:
            self.skipTest("fixture roster is not a file here")
        roster.write_text(text.replace('title = "Morning report"', 'priority = "high"\ntitle = "Morning report"'))
        self.state(10)
        self.run_dispatch("routines")
        self.assertEqual([self.stub.labels(i["number"]) for i in self.stub.issues.values()],
                         [{"agent:saito", "status:todo", "priority:high"}])

    def test_first_run_does_not_backfill(self):
        self.run_dispatch("routines")
        self.assertEqual(self.stub.issues, {})

    def test_cron(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Stockholm")
        at = lambda h, m, d=1: dt.datetime(2026, 11, d, h, m, tzinfo=tz)   # 1 Nov 2026 is a Sunday
        self.assertTrue(dispatch.cron_matches("40 5,16 * * *", at(16, 40)))
        self.assertFalse(dispatch.cron_matches("40 5,16 * * *", at(16, 41)))
        self.assertTrue(dispatch.cron_matches("0 1 * * 0", at(1, 0)))
        self.assertFalse(dispatch.cron_matches("0 1 * * 1-5", at(1, 0)))
        self.assertTrue(dispatch.cron_matches("0 1 * * 1-5", at(1, 0, 2)))
        self.assertTrue(dispatch.cron_matches("*/15 * * * *", at(3, 45)))


class TestS9Task(Base):
    def s9(self, *args, home=None):
        env = {"PATH": os.environ["PATH"], "S9_API_URL": self.url + "/gh", "HOME": home or str(self.t),
               "GH_TOKEN": GH}
        if home:
            del env["GH_TOKEN"]
        p = subprocess.run([sys.executable, "-I", str(ROOT / "sandbox" / "s9-task"), *args],
                           capture_output=True, text=True, env=env, input="from stdin")
        self.assertNotIn(GH, p.stdout + p.stderr)
        return p

    def test_comment_and_status_on_own_issue(self):
        n = self.stub.add(["agent:batou", "status:working"])
        self.assertEqual(self.s9("--as", "Batou", "comment", str(n), "-").returncode, 0)
        self.assertEqual(self.stub.comments, [(n, "from stdin")])
        self.assertEqual(self.s9("--as", "Batou", "status", str(n), "review", "PR: x").returncode, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.comments[-1], (n, "PR: x"))
        self.assertEqual(self.stub.issues[n]["state"], "open")
        self.s9("--as", "Batou", "status", str(n), "done")
        self.assertEqual(self.stub.issues[n]["state"], "closed")

    def test_refuses_other_agents_issues_and_bad_input(self):
        n = self.stub.add(["agent:ishikawa", "status:working"])
        p = self.s9("--as", "Batou", "status", str(n), "done")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("not labelled agent:batou", p.stderr)
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:working"})
        self.assertNotEqual(self.s9("--as", "Batou", "status", str(n), "cancelled").returncode, 0)
        self.assertNotEqual(self.s9("comment", str(n), "x").returncode, 0)          # no --as

    def test_token_from_git_credentials_and_never_printed(self):
        home = self.t / "home"
        home.mkdir()
        (home / ".git-credentials").write_text(f"https://x-access-token:{GH}@github.com\n")
        n = self.stub.add(["agent:batou", "status:working"])
        self.assertEqual(self.s9("--as", "Batou", "comment", str(n), "hi", home=str(home)).returncode, 0)
        p = self.s9("--as", "Batou", "comment", "99", "hi", home=str(home))    # stub echoes the auth header
        self.assertEqual(p.returncode, 1)
        self.assertIn("[token]", p.stderr)


class TestInstall(unittest.TestCase):
    def test_install_sets_the_authorized_keys_file_to_0644_root(self):
        # sshd in the sandbox refused the 0600 root-owned file (fixed by hand on 31 Oct).
        text = (ROOT / "vps" / "install.sh").read_text()
        self.assertIn('chmod 0644 "$secrets/sandbox_authorized_keys"', text)
        self.assertIn('chown root:root "$secrets/sandbox_authorized_keys"', text)
        # ... after the secrets check (which only requires the file to exist), before compose up
        self.assertLess(text.index("0644"), text.index("docker compose"))

    def test_install_enables_every_timer_through_install_units(self):
        self.assertIn("install-units.sh", (ROOT / "vps" / "install.sh").read_text())
        units = (ROOT / "vps" / "install-units.sh").read_text()
        for timer in (ROOT / "vps" / "systemd").glob("*.timer"):
            self.assertIn(timer.name, units)


class TestSync(unittest.TestCase):
    """vps/sync.sh against a local origin, a stub CI reply and fake docker."""

    def setUp(self):
        self.stub = Stub()
        self.srv = serve(self.stub)
        self.tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self.tmp.name)
        self.env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                    "S9_REPO_DIR": str(t / "repo"), "S9_STATE_DIR": str(t / "state"), "S9_LOCK_FILE": str(t / "lock"),
                    "S9_GITHUB_API": f"http://127.0.0.1:{self.srv.server_address[1]}/ci",
                    "S9_PINS_FILE": str(t / "pins.toml"), "S9_SECRETS_DIR": str(t / "secrets"),
                    "S9_DISPATCH_PY": str(ROOT / "vps" / "dispatch.py"), "S9_SYNC_CONFIG": str(t / "sync.toml"),
                    "S9_SOAK_MINUTES": "0",        # the soak has its own tests
                    "PATH": f"{t / 'bin'}:{os.environ['PATH']}"}
        (t / "secrets").mkdir()
        (t / "secrets" / "sandbox.env").write_text(f"GH_TOKEN={GH}\n")
        (t / "secrets" / "hermes.env").write_text(f"OPENROUTER_API_KEY={ORKEY}\nAPI_SERVER_KEY={HKEY}\n")
        (t / "bin").mkdir()
        (t / "bin" / "docker").write_text(f'#!/bin/sh\necho "docker $@" >> {t}/calls\n[ ! -e {t}/docker-fails ]\n')
        (t / "bin" / "docker").chmod(0o755)
        # A healthy host: the timers are active and fired a minute ago, the journals are quiet.
        (t / "bin" / "systemctl").write_text(
            '#!/bin/sh\ncase "$1" in\n is-active) cat "$0.active" 2>/dev/null || echo active;;\n'
            ' show) case "$*" in *LastTriggerUSec*) date -u -d "${S9_FAKE_AGE:-1} minutes ago" "+%a %Y-%m-%d %H:%M:%S UTC";; *) echo success;; esac;;\nesac\n')
        (t / "bin" / "journalctl").write_text('#!/bin/sh\ncat "$0.out" 2>/dev/null || true\n')
        for f in ("systemctl", "journalctl"):
            (t / "bin" / f).chmod(0o755)
        self.git("init", "-q", "-b", "main", str(t / "origin"), cwd=t)
        self.commit({"vps/install-units.sh": f'#!/bin/sh\necho "install-units" >> {t}/calls\n',
                     "vps/dashboard-prep.sh": f'#!/bin/sh\necho "dashboard-prep $S9_STATE_DIR" >> {t}/prep\n', "README.md": "x"})
        self.git("clone", "-q", str(t / "origin"), str(t / "repo"), cwd=t)
        self.checks = [{"name": n, "status": "completed", "conclusion": "success"} for n in (
            "Secret scan", "Compose and config", "Sandbox image smoke test", "Roster script tests", "Deploy scripts", "VPS stack")]
        self.stub.checks = self.checks

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    def git(self, *args, cwd):
        subprocess.run(["git", *args], cwd=cwd, env=self.env, check=True, capture_output=True)

    def commit(self, files, when=None):
        for name, text in files.items():
            f = self.t / "origin" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            if name.endswith(".sh"):
                f.chmod(0o755)
        self.git("add", "-A", cwd=self.t / "origin")
        if when is not None:        # the committer time, i.e. when the squash merge happened
            self.env["GIT_COMMITTER_DATE"] = when
        self.git("commit", "-q", "-m", "change", cwd=self.t / "origin")
        self.env.pop("GIT_COMMITTER_DATE", None)

    def accept(self):
        """What Petter runs on the VPS after reading a change to a guarded path."""
        return subprocess.run(["bash", str(ROOT / "vps" / "sync.sh"), "--accept"], env=self.env, capture_output=True, text=True)

    def sync(self):
        p = subprocess.run(["bash", str(ROOT / "vps" / "sync.sh")], env=self.env, capture_output=True, text=True)
        calls = (self.t / "calls").read_text().splitlines() if (self.t / "calls").exists() else []
        return p, calls

    def sync_issue(self):
        return next(i for i in self.stub.issues.values() if i["title"] == "Sync")

    def origin_head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.t / "origin", capture_output=True, text=True).stdout.strip()

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.t / "repo", capture_output=True, text=True).stdout.strip()

    def test_nothing_new_does_nothing(self):
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls, p.stdout), (0, [], ""))

    def test_waits_while_ci_is_not_green(self):
        self.commit({"vps/compose.yaml": "x"})
        before = self.head()
        self.checks[0]["conclusion"] = "failure"
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls, self.head()), (0, [], before))
        self.assertIn("waiting for CI", p.stdout)
        self.assertIn("Secret scan (failure)", p.stdout)
        self.checks[0].update(conclusion="success")
        self.checks.pop()          # a required check that never ran
        p, calls = self.sync()
        self.assertEqual((calls, self.head()), ([], before))
        self.assertIn("missing: VPS stack", p.stdout)

    def test_compose_change_runs_compose_up_once(self):
        self.commit({"vps/compose.yaml": "x"})
        self.accept()
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual((self.t / "prep").read_text().split(), ["dashboard-prep", str(self.t / "state")])   # the login exists before Hermes starts
        self.assertTrue(calls[0].startswith("docker compose -f ") and calls[0].endswith("vps/compose.yaml up -d --build --remove-orphans"))
        # then the leftover Impeccable skill goes from the Hermes volume
        self.assertTrue(calls[1].endswith("vps/compose.yaml exec -T hermes rm -rf /opt/data/skills/impeccable"), calls[1])
        self.assertEqual(self.head(), subprocess.run(["git", "rev-parse", "main"], cwd=self.t / "origin", capture_output=True, text=True).stdout.strip())
        self.assertIn("applied", p.stdout)
        self.assertEqual(self.sync()[1], calls)     # applied: next tick is a no-op

    def test_unit_change_reinstalls_units_and_docs_change_does_nothing(self):
        self.commit({"vps/systemd/section9-x.timer": "x"})
        self.accept()
        self.assertEqual(self.sync()[1], ["install-units"])
        (self.t / "calls").unlink()
        self.commit({"docs/RUNBOOK.md": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (0, []))
        self.assertIn("applied", p.stdout)

    def test_hermes_or_sandbox_change_rebuilds(self):
        self.commit({"sandbox/Dockerfile": "x"})
        self.accept()
        self.assertEqual(len(self.sync()[1]), 2)        # up, then the Impeccable prune
        self.commit({"hermes/config.yaml": "x"})
        self.accept()
        self.assertEqual(len(self.sync()[1]), 4)

    def test_failed_apply_is_retried_three_times_then_stops_until_main_moves(self):
        (self.t / "docker-fails").write_text("")
        self.commit({"vps/compose.yaml": "x"})
        self.accept()
        for _ in range(3):
            self.assertEqual(self.sync()[0].returncode, 1)
        self.assertEqual(len(self.sync()[1]), 3)
        p, calls = self.sync()
        self.assertEqual((p.returncode, len(calls)), (1, 3))
        self.assertIn("not retrying", p.stderr)
        (self.t / "docker-fails").unlink()
        self.commit({"vps/compose.yaml": "y"})      # main moves: tries again, from the last applied commit
        self.accept()
        p, calls = self.sync()
        self.assertEqual((p.returncode, len(calls)), (0, 5))

    def test_dirty_checkout_is_refused_and_reported_on_the_board(self):
        (self.t / "repo" / "README.md").write_text("hand edit")
        self.commit({"vps/compose.yaml": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (1, []))
        self.assertIn("local changes", p.stderr)
        issue = self.sync_issue()
        self.assertEqual({l["name"] for l in issue["labels"]}, {"status:blocked"})
        self.assertIn("local changes", issue["body"])
        self.assertIn("README.md", issue["body"])

    def test_protected_change_is_refused_until_a_human_pins_it(self):
        before = self.head()
        self.commit({"vps/install.sh": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (1, []))
        self.assertIn("does not pin", p.stderr)
        self.assertEqual(self.head(), before)       # the checkout is untouched
        issue = self.sync_issue()
        self.assertEqual({l["name"] for l in issue["labels"]}, {"status:blocked"})
        self.assertIn("vps/install.sh is new", issue["body"])
        self.assertIn("--accept", issue["body"])

    def test_accept_pins_what_a_human_read_and_the_sync_then_applies(self):
        self.commit({"vps/install.sh": "x", "vps/compose.yaml": "y"})
        p = self.accept()
        self.assertEqual(p.returncode, 0, p.stderr)
        pins = (self.t / "pins.toml").read_text()
        self.assertIn("vps/install.sh", pins)
        self.assertNotIn("vps/compose.yaml", pins)      # not protected: never needs a pin
        self.assertEqual(oct((self.t / "pins.toml").stat().st_mode & 0o777), "0o600")
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)             # up, then the Impeccable prune
        self.assertIn("Applied", self.sync_issue()["body"])

    def test_a_normal_change_applies_by_itself_without_any_pin(self):
        """The point of the PR: compose, units, scripts and sandbox changes need no --accept."""
        self.commit({"vps/compose.yaml": "x", "vps/systemd/section9-routines.timer": "t", "sandbox/Dockerfile": "d",
                     "vps/hq-status.py": "p"})
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 3)             # compose up, the Impeccable prune, install-units
        self.assertFalse((self.t / "pins.toml").exists())
        self.assertEqual(self.head(), self.origin_head())
        self.assertIn("Applied", self.sync_issue()["body"])

    def test_the_protected_set_is_the_sync_its_installers_its_units_workflows_and_sudoers(self):
        # (vps/sync.sh itself is covered by the replay test: a bogus copy could not run.)
        for path in ("vps/install.sh", "vps/install-units.sh", "vps/systemd/section9-sync.timer",
                     "vps/systemd/section9-sync.service", ".github/workflows/ci.yml", "vps/sudoers.d/section9"):
            before = self.head()
            self.commit({path: "#!/bin/sh\n:\n"})
            p, calls = self.sync()
            self.assertEqual((p.returncode, calls, self.head()), (1, [], before), path)
            self.assertIn("does not pin", p.stderr, path)
            self.assertEqual(self.accept().returncode, 0)
            self.assertEqual(self.sync()[0].returncode, 0, path)      # pinned: the same commit applies
            (self.t / "calls").unlink(missing_ok=True)

    def test_a_pinned_protected_file_that_changes_again_needs_a_new_pin(self):
        self.commit({"vps/install.sh": "one"})
        self.accept()
        self.assertEqual(self.sync()[0].returncode, 0)
        self.commit({"vps/install.sh": "two"})
        p, _ = self.sync()
        self.assertEqual(p.returncode, 1)
        self.assertIn("vps/install.sh changed", p.stderr)

    def test_the_root_only_config_can_protect_more_but_not_less(self):
        (self.t / "sync.toml").write_text('protect = ["vps/hq-status.py"]\nsoak_minutes = 0\n')
        self.commit({"vps/hq-status.py": "x"})
        p, _ = self.sync()
        self.assertEqual(p.returncode, 1)
        self.assertIn("vps/hq-status.py is new", p.stderr)
        self.commit({"vps/install.sh": "x"})        # the defaults stay, whatever the file says
        self.assertEqual(self.sync()[0].returncode, 1)

    def test_an_unreadable_config_fails_closed_and_is_reported(self):
        (self.t / "sync.toml").write_text("soak_minutes = [\n")
        self.commit({"docs/RUNBOOK.md": "x"})
        before = self.head()
        p, calls = self.sync()
        self.assertEqual((p.returncode, self.head()), (1, before))
        self.assertIn("Refusing to sync", self.sync_issue()["body"])

    def test_ci_red_is_refused_even_for_an_unprotected_change(self):
        before = self.head()
        self.commit({"docs/RUNBOOK.md": "x"})
        self.checks[3]["conclusion"] = "failure"
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls, self.head()), (0, [], before))
        self.assertIn("waiting for CI", p.stdout)
        self.assertIn("not green: Roster script tests", p.stdout)
        self.checks[3]["conclusion"] = "success"
        self.assertEqual(self.sync()[0].returncode, 0)
        self.assertNotEqual(self.head(), before)

    def test_ci_must_have_every_required_check_not_just_the_ones_that_ran(self):
        before = self.head()
        self.commit({"docs/RUNBOOK.md": "x"})
        del self.checks[-1]                          # "VPS stack" never reported
        p, _ = self.sync()
        self.assertEqual((p.returncode, self.head()), (0, before))
        self.assertIn("missing: VPS stack", p.stdout)

    def test_a_force_pushed_main_is_never_applied(self):
        self.commit({"docs/RUNBOOK.md": "x"})
        self.assertEqual(self.sync()[0].returncode, 0)
        applied = self.head()
        # Rewrite history: a new main that does not contain the applied commit.
        self.git("reset", "-q", "--hard", "HEAD~1", cwd=self.t / "origin")
        self.commit({"README.md": "rewritten"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls, self.head()), (1, [], applied))
        self.assertIn("not a descendant", p.stderr)

    def test_a_fresh_merge_soaks_and_an_old_enough_one_applies(self):
        self.env["S9_SOAK_MINUTES"] = "30"
        before = self.head()
        now = int(time.time())
        self.commit({"vps/compose.yaml": "x"}, when=f"{now - 10 * 60} +0000")
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls, self.head()), (0, [], before))
        self.assertIn("soak", p.stdout)
        self.assertIn("soaking", self.sync_issue()["body"])
        self.env["S9_SOAK_MINUTES"] = "5"       # ten minutes old is past a 5 min soak
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.head(), self.origin_head())

    def test_a_revert_inside_the_soak_means_the_bad_commit_never_lands(self):
        self.env["S9_SOAK_MINUTES"] = "30"
        base = self.head()
        self.commit({"vps/compose.yaml": "bad"}, when=f"{int(time.time()) - 5 * 60} +0000")
        self.assertEqual(self.sync()[1], [])
        self.git("revert", "--no-edit", "HEAD", cwd=self.t / "origin")      # the Major reverts it
        # Both are young: nothing lands. When the revert has aged, the checkout still equals base in content.
        self.env["S9_SOAK_MINUTES"] = "0"
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual((self.t / "repo" / "vps" / "compose.yaml").exists(), False)
        self.assertEqual(self.head(), self.origin_head())
        self.assertNotEqual(self.head(), base)

    def test_the_sync_issue_carries_timer_health_and_flags_a_stalled_routines_timer(self):
        (self.t / "bin" / "journalctl.out").write_text("routines: error: private routines not loaded (TOMLDecodeError)\n")
        self.env["S9_FAKE_AGE"] = "300"
        p, _ = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        body = self.sync_issue()["body"]
        self.assertIn("section9-routines.timer", body)
        self.assertIn("section9-dispatch.timer", body)
        self.assertIn("STALLED (no run for over 30 min)", body)
        self.assertIn("private routines not loaded", body)
        self.assertIn("Up to date", body)
        (self.t / "bin" / "systemctl.active").write_text("inactive\n")
        self.sync()
        self.assertIn("STALLED (timer is not active)", self.sync_issue()["body"])

    def test_an_idle_tick_does_not_rewrite_the_issue_when_nothing_changed(self):
        self.sync()
        issue = self.sync_issue()["number"]
        before = self.stub.issues[issue]["body"]
        self.sync()
        self.assertEqual((self.stub.issues[issue]["body"], len(self.stub.issues)), (before, 1))   # same "As of" time: untouched

    def test_an_unguarded_change_needs_no_pins_at_all(self):
        self.commit({"docs/RUNBOOK.md": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (0, []))
        self.assertIn("applied", p.stdout)

    def test_a_commit_that_changes_sync_sh_is_applied_again_by_the_new_copy(self):
        # The new copy's own new step, as #46's dashboard-prep.sh line was for the old
        # sync.sh: only the re-run can reach it.
        marker = f'  echo "new-step" >> {self.t}/marker\n'
        text = (ROOT / "vps" / "sync.sh").read_text().replace(
            '  changed=$(git diff --name-only "$1" "$2")\n', '  changed=$(git diff --name-only "$1" "$2")\n' + marker)
        self.assertIn(marker, text)
        self.commit({"vps/sync.sh": text, "vps/compose.yaml": "x"})
        self.accept()
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("re-running it", p.stdout)
        self.assertEqual((self.t / "marker").read_text(), "new-step\n")
        self.assertEqual(len(calls), 4)             # once per copy: compose-up, then the Impeccable prune


class TestGuardrails(Base):
    """The root-only limits file: what the dispatcher refuses to take from the repo."""

    def limits(self, text="", brief=None):
        (self.t / "limits.toml").write_text(text)
        if brief is not None:
            (self.t / "secrets" / "briefs").mkdir(exist_ok=True)
            (self.t / "secrets" / "briefs" / "Batou.md").write_text(brief)
        return str(self.t / "limits.toml")

    def system_message(self, limits):
        self.stub.add(["agent:batou", "status:todo"])
        code, _ = self.run_dispatch("run", limits=limits)
        self.assertEqual(code, 0)
        return self.stub.hermes_calls[0][0]["messages"][0]["content"]

    def test_the_preamble_and_a_locked_brief_win_over_the_repo(self):
        limits = self.limits('preamble = "Read-only outside your task."\n', brief="You are Batou, locked brief.")
        self.assertEqual(self.system_message(limits),
                         "Read-only outside your task.\n\n---\n\nYou are Batou, locked brief.")

    def test_without_a_limits_file_the_repo_brief_is_used_as_before(self):
        system = self.system_message(str(self.t / "none-limits.toml"))
        self.assertIn("You are **Batou**", system)
        self.assertNotIn("Read-only", system)

    def test_a_run_is_cut_off_at_the_root_only_ceiling_not_the_roster_value(self):
        limits = self.limits("[timeout_sec]\nBatou = 1\n")      # the roster says 60 s
        self.stub.hermes = "slow"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch("run", limits=limits)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("timed out at 1 s", self.stub.comments[0][1])

    def test_the_ceiling_is_the_root_only_value_and_the_roster_can_only_lower_it(self):
        self.assertEqual(dispatch.ceiling({"name": "Batou", "timeout_sec": 3600}, {"Batou": 60}), 60)
        self.assertEqual(dispatch.ceiling({"name": "Ishikawa", "timeout_sec": 60}, {"default": 30}), 30)
        self.assertEqual(dispatch.ceiling({"name": "Saito", "timeout_sec": 60}, {}), 60)
        caps, preamble = dispatch.limits_of(self.t / "gone.toml")
        self.assertEqual((caps, preamble), ({}, ""))
        self.assertEqual(dispatch.limits_of(self.limits('preamble = "p"\n[timeout_sec]\ndefault = 5\n')),
                         ({"default": 5}, "p"))


class TestHq(Base):
    def run_hq(self, cmd, site=None):
        argv = [cmd, "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--site-dir", str(site or self.t / "site"), "--themes-dir", str(self.t / "themes"), "--repo-root", str(ROOT),
                "--github", self.url + "/gh", "--openrouter", self.url + "/or"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = hq.main(argv)
        for text in (out.getvalue(), err.getvalue()):
            for secret in (GH, ORKEY):
                self.assertNotIn(secret, text)
        return code, err.getvalue()

    def status(self):
        return json.loads((self.t / "site" / "status.json").read_text())

    def agent(self, doc, name):
        return next(a for a in doc["agents"] if a["name"] == name)

    def board(self):
        self.stub.add(["agent:batou", "status:working"], "Fix the crew page layout")
        self.stub.add(["agent:ishikawa", "status:working", "private"], "Look at private errand")
        self.stub.add(["agent:togusa", "status:working"], "Check the secret-garden flow")
        pr = self.stub.add(["agent:batou", "status:review"], "Add dark mode")
        self.stub.issues[pr]["closed_at"] = None
        self.stub.gh_comments = [{"issue_url": f"https://api.github.com/repos/x/y/issues/{pr}",
                                  "body": "PR: https://github.com/petterfranzen/ghost-in-the-machine/pull/12 done"}]
        done = self.stub.add(["agent:tachikoma", "status:done"], "Nightly build")
        self.stub.issues[done]["state"] = "closed"
        self.stub.add(["agent:saito", "status:done", "private"], "Morning ops report")

    def test_status_keeps_the_crew_format_and_hides_private_tasks(self):
        self.board()
        (self.t / "secrets" / "crew-deny.txt").write_text("# private topics\nsecret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        doc = self.status()
        self.assertEqual(set(doc), {"generatedAt", "budget", "model", "agents", "tasks"})
        self.assertEqual(doc["budget"], {"usedPercent": 25, "month": doc["budget"]["month"]})     # 5 of 20, no dollars
        self.assertEqual(doc["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual([a["name"] for a in doc["agents"]], ["Tachikoma", "Batou", "Ishikawa", "Togusa", "Saito"])
        for a in doc["agents"]:
            self.assertEqual(set(a), {"name", "role", "state", "now", "lastActiveAt", "budgetPercent", "recent"})
        batou = self.agent(doc, "Batou")
        self.assertEqual((batou["state"], batou["now"]), ("working", "Fix the crew page layout"))
        self.assertEqual(batou["recent"], [{"at": "2026-11-01T10:00:00Z", "text": "Add dark mode",
                                            "url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/12"}])
        self.assertEqual(self.agent(doc, "Ishikawa")["now"], "On a private task")       # labelled private
        self.assertEqual(self.agent(doc, "Togusa")["now"], "On a private task")         # deny term
        self.assertEqual(self.agent(doc, "Tachikoma")["state"], "idle")
        self.assertEqual(self.agent(doc, "Saito")["recent"][0]["text"], "On a private task")
        self.assertNotIn("url", self.agent(doc, "Saito")["recent"][0])
        raw = json.dumps(doc)
        for leak in ("private errand", "secret-garden", "Morning ops", "$", "sk-or"):
            self.assertNotIn(leak, raw)          # section9-hq appears only in a public task's url

    def test_tasks_carry_status_agent_and_a_public_pr_and_nothing_private(self):
        self.board()
        (self.t / "secrets" / "crew-deny.txt").write_text("secret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        tasks = self.status()["tasks"]
        by_title = {t["title"]: t for t in tasks}
        for t in tasks:
            self.assertEqual(set(t), {"id", "title", "agent", "status", "waits", "waitsText", "url", "pr", "updated"})
        self.assertEqual((by_title["Fix the crew page layout"]["status"], by_title["Fix the crew page layout"]["agent"]),
                         ("working", "batou"))
        self.assertEqual(by_title["Add dark mode"]["status"], "review")
        self.assertEqual(by_title["Add dark mode"]["pr"], "https://github.com/petterfranzen/ghost-in-the-machine/pull/12")
        self.assertEqual(by_title["Add dark mode"]["url"], "https://github.com/petterfranzen/section9-hq/issues/4")
        self.assertEqual(by_title["Nightly build"]["status"], "done")
        private = [t for t in tasks if t["title"] == "On a private task"]
        self.assertEqual(len(private), 3)       # the label, the deny term, the private report
        self.assertTrue(all(t["pr"] is None and t["url"] is None for t in private))
        raw = json.dumps(tasks)
        for leak in ("private errand", "secret-garden", "Morning ops", "$"):
            self.assertNotIn(leak, raw)

    def test_without_a_deny_file_task_titles_are_not_published(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        self.assertEqual({t["title"] for t in self.status()["tasks"]}, {"On a task", "On a private task"})

    def test_status_writes_the_live_data_and_installs_the_vendored_page(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        site = self.t / "site"
        live = (site / "status.json").read_text()
        self.assertEqual((site / "board" / "board.json").read_text(), live)     # what the waits page polls
        self.assertEqual((site / "board" / "status.json").read_text(), live)    # the placeholder's feed
        self.assertEqual((site / "board" / "index.html").read_bytes(), (ROOT / "vps" / "board" / "index.html").read_bytes())
        self.assertEqual((site / "board" / "board-sample.json").read_bytes(),
                         (ROOT / "vps" / "board" / "board-sample.json").read_bytes())

    def test_the_waits_column_comes_from_the_waits_label_and_the_line_from_the_comment(self):
        petter = self.stub.add(["agent:batou", "status:todo", "waits:petter"], "Decide the palette")
        qa = self.stub.add(["agent:togusa", "status:review", "waits:qa"], "QA the crew page")
        plain = self.stub.add(["agent:batou", "status:working"], "Tidy the README")          # no label -> agent
        unknown = self.stub.add(["agent:batou", "status:working", "waits:someday"], "Guess")  # unknown -> agent
        self.stub.gh_comments = [
            {"issue_url": f"https://api.github.com/repos/x/y/issues/{petter}", "body": "Waits on the earlier ask"},
            {"issue_url": f"https://api.github.com/repos/x/y/issues/{petter}", "body": "Waits on a yes or no on the palette\nsecond line"},
            {"issue_url": f"https://api.github.com/repos/x/y/issues/{qa}", "body": "first\n  Waits on the crew PR to be checked"},
            {"issue_url": f"https://api.github.com/repos/x/y/issues/{plain}", "body": "Waits on the palette decision (#101)"},
        ]
        (self.t / "secrets" / "crew-deny.txt").write_text("secret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        by = {t["id"]: t for t in self.status()["tasks"]}
        self.assertEqual((by[petter]["waits"], by[petter]["waitsText"]),
                         ("petter", "Waits on a yes or no on the palette"))     # the latest comment wins
        self.assertEqual((by[qa]["waits"], by[qa]["waitsText"]), ("qa", "Waits on the crew PR to be checked"))
        self.assertEqual((by[plain]["waits"], by[plain]["waitsText"]),
                         ("agent", "Waits on the palette decision (#101)"))
        self.assertEqual((by[unknown]["waits"], by[unknown]["waitsText"]), ("agent", ""))

    def test_a_private_task_gets_no_url_and_no_waits_line(self):
        n = self.stub.add(["agent:batou", "status:working", "private"], "Look at private errand")
        self.stub.gh_comments = [{"issue_url": f"https://api.github.com/repos/x/y/issues/{n}",
                                  "body": "Waits on the private errand details"}]
        (self.t / "secrets" / "crew-deny.txt").write_text("secret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        (t,) = self.status()["tasks"]
        self.assertEqual((t["title"], t["url"], t["waitsText"]), ("On a private task", None, ""))

    def test_the_page_falls_back_to_the_placeholder_when_the_real_one_is_missing(self):
        root = self.t / "root"
        (root / "vps" / "board").mkdir(parents=True)
        (root / "vps" / "board" / "placeholder.html").write_text("PLACEHOLDER")
        self.assertEqual(hq.board_assets(root), (b"PLACEHOLDER", None))
        (root / "vps" / "board" / "index.html").write_text("REAL")
        (root / "vps" / "board" / "board-sample.json").write_text("{}")
        self.assertEqual(hq.board_assets(root), (b"REAL", b"{}"))
        (root / "vps" / "board" / "index.html").unlink()
        (root / "vps" / "board" / "placeholder.html").unlink()
        with self.assertRaises(hq.Failure):
            hq.board_assets(root)

    def test_without_a_deny_file_only_states_and_on_a_task(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        doc = self.status()
        for a in doc["agents"]:
            self.assertEqual(a["recent"], [])
            self.assertIn(a["now"], (None, "On a task"))
        self.assertEqual(self.agent(doc, "Batou")["state"], "working")
        self.assertNotIn("crew page", json.dumps(doc))

    def test_owner_name_never_published_and_failure_keeps_the_old_file(self):
        (self.t / "secrets" / "crew-deny.txt").write_text("zzz\n")
        self.stub.add(["agent:batou", "status:working"], "Email Petter about it")
        self.run_hq("status")
        self.assertEqual(self.agent(self.status(), "Batou")["now"], "On a private task")
        before = (self.t / "site" / "status.json").read_text()
        (self.t / "secrets" / "sandbox.env").write_text("GH_TOKEN=github_pat_" + "x" * 20 + "\n")    # GitHub says 401
        code, err = self.run_hq("status")
        self.assertEqual(code, 1)
        self.assertIn("401", err)
        self.assertEqual((self.t / "site" / "status.json").read_text(), before)

    def test_model_is_read_from_the_config(self):
        self.assertEqual(hq.model_of((ROOT / "hermes" / "config.yaml").read_text()), "deepseek/deepseek-v4.1-flash")
        self.assertEqual(hq.model_of("model:\n  default: a/b # c\nx: 1\n"), "a/b")

    def test_crew_mirrors_main_without_a_token_and_keeps_the_live_status(self):
        site = self.t / "site"
        site.mkdir()
        (site / "status.json").write_text("LIVE")
        (site / "old.html").write_text("gone soon")
        self.assertEqual(self.run_hq("crew")[0], 0)
        self.assertIsNone(self.stub.crew_auth)                          # public repo: no Authorization header
        self.assertEqual((site / "index.html").read_text(), "<h1>crew</h1>")
        self.assertEqual((site / "css" / "app.css").read_text(), "a{}")
        self.assertEqual((site / "status.json").read_text(), "LIVE")    # the sample in crew/ is not copied
        self.assertFalse((site / "old.html").exists())
        self.assertEqual(oct((site / "index.html").stat().st_mode & 0o777), "0o644")
        (site / "index.html").write_text("touched")                     # same tree sha: nothing fetched
        self.run_hq("crew")
        self.assertEqual((site / "index.html").read_text(), "touched")
        self.stub.crew_sha, self.stub.crew["index.html"] = "tree2", "<h1>new</h1>"
        self.run_hq("crew")
        self.assertEqual((site / "index.html").read_text(), "<h1>new</h1>")

    def test_the_crew_sync_leaves_the_board_folder_alone(self):
        site = self.t / "site"
        (site / "board").mkdir(parents=True)
        (site / "board" / "index.html").write_text("BOARD")
        (site / "board" / "status.json").write_text("LIVE")
        self.assertEqual(self.run_hq("crew")[0], 0)
        self.assertEqual((site / "board" / "index.html").read_text(), "BOARD")
        self.assertEqual((site / "board" / "status.json").read_text(), "LIVE")

    def test_crew_without_an_index_changes_nothing(self):
        site = self.t / "site"
        site.mkdir()
        (site / "index.html").write_text("kept")
        del self.stub.crew["index.html"]
        code, err = self.run_hq("crew")
        self.assertEqual(code, 1)
        self.assertEqual((site / "index.html").read_text(), "kept")

    def themes(self):
        return {p.name: p.read_text() for p in (self.t / "themes").glob("*")}

    def test_themes_come_from_hq_yaml_on_main_and_only_top_level_yaml(self):
        (self.t / "themes").mkdir()
        (self.t / "themes" / "stale.yaml").write_text("name: stale\n")
        self.assertEqual(self.run_hq("crew")[0], 0)
        self.assertEqual(self.themes(), {"ghost.yaml": "name: ghost\nlabel: Batou's\n"})   # no .md, no sub/, stale removed
        self.assertEqual(oct((self.t / "themes" / "ghost.yaml").stat().st_mode & 0o777), "0o644")
        self.stub.hq_sha, self.stub.hq["ghost.yaml"] = "hq2", "name: ghost\nlabel: new\n"
        self.run_hq("crew")
        self.assertIn("label: new", self.themes()["ghost.yaml"])

    def test_without_hq_on_main_the_starter_ghost_theme_is_used(self):
        self.stub.hq = None                                  # GitHub answers 404
        code, err = self.run_hq("crew")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.themes(), {"ghost.yaml": (ROOT / "vps" / "dashboard-themes" / "ghost.yaml").read_text()})
        self.assertTrue((self.t / "site" / "index.html").exists())      # the crew page was fetched as well

    def test_a_bad_theme_keeps_what_is_served_and_does_not_stop_the_crew_page(self):
        self.run_hq("crew")
        self.stub.hq_sha, self.stub.hq["ghost.yaml"] = "hq2", "just text"
        self.stub.crew_sha, self.stub.crew["index.html"] = "tree2", "<h1>new</h1>"
        code, err = self.run_hq("crew")
        self.assertEqual(code, 1)
        self.assertIn("no `name:`", err)
        self.assertIn("label: Batou", self.themes()["ghost.yaml"])
        self.assertEqual((self.t / "site" / "index.html").read_text(), "<h1>new</h1>")

    def test_starter_theme_is_a_valid_ghost_theme(self):
        text = (ROOT / "vps" / "dashboard-themes" / "ghost.yaml").read_text()
        self.assertRegex(text, r"(?m)^name: ghost$")
        for part in ("palette:", "typography:", "customCSS: |", "repeating-linear-gradient", "monospace"):
            self.assertIn(part, text)
        self.assertLess(len(text), 32 << 10)                 # Hermes caps customCSS at 32 KiB


class TestLedger(Base):
    def ledger(self):
        path = self.t / "state" / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_one_json_line_per_run_with_agent_type_tokens_cost_and_cpu(self):
        self.stub.bump = 0.03
        n = self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.assertEqual(self.run_dispatch()[0], 0)
        (row,) = self.ledger()
        # The model is what the run asked for (the gateway default here), plus the model it was booked to.
        self.assertEqual((row["v"], row["issue"], row["agent"], row["role"], row["type"], row["model"]),
                         (1, n, "Batou", "", "qa", dispatch.DEFAULT_MODEL))
        self.assertEqual(row["model_requested"], dispatch.DEFAULT_MODEL)
        self.assertEqual((row["outcome"], row["queue_before"]), ("ok", 1))
        self.assertGreaterEqual(row["seconds"], 0)
        self.assertEqual(row["cost_usd"], 0.03)
        self.assertEqual(row["usage_before"]["usage_monthly"], 5.0)
        self.assertEqual(row["usage_after"]["usage_monthly"], 5.03)
        self.assertEqual(row["tokens"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertEqual(set(row["cpu"]), {"wall_seconds", "steal_pct", "core_seconds", "credits"})
        self.assertEqual(set(row["cpu_before"]), {"at", "busy", "steal", "total"})
        self.assertGreaterEqual(row["cpu_after"]["total"], row["cpu_before"]["total"])
        self.assertGreaterEqual(row["cpu"]["core_seconds"], 0)
        self.assertTrue(row["start"].endswith("+00:00") and row["end"].endswith("+00:00"))

    def test_outcomes_are_named_not_guessed(self):
        cases = (("silent", "no-output"), ("error", "blocked"))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                self.setUp()
                self.stub.hermes = mode
                self.stub.add(["agent:batou", "status:todo"])
                self.run_dispatch()
                self.assertEqual(self.ledger()[0]["outcome"], expected)
                self.tearDown()

    def test_a_timeout_is_recorded_as_a_timeout_with_its_hours(self):
        (self.t / "roster.toml").write_text(ROSTER.format(timeout=1))
        self.stub.hermes = "slow"
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        row = self.ledger()[0]
        self.assertEqual(row["outcome"], "timeout")
        self.assertGreaterEqual(row["seconds"], 1.0)      # the 1 s run ceiling ends it; the record says so

    def test_a_broken_ledger_never_stops_the_run(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatch, "append_ledger", side_effect=OSError("disk full")):
            code, out = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        self.assertIn("ledger: not written", out)

    def test_an_issue_without_a_readable_cost_is_not_given_one(self):
        self.stub.key = {"data": {"usage": 5, "limit": 20, "limit_remaining": 15,
                                  "usage_monthly": None, "usage_daily": 0.25}}
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        row = self.ledger()[0]
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["usage_before"], {"usage": 5, "usage_monthly": None, "usage_daily": 0.25,
                                               "limit": 20, "limit_remaining": 15})

    def test_a_failing_key_read_still_runs_and_records_no_cost(self):
        self.stub.key_fail_after = 1        # spend_ok's read succeeds; the ledger's two reads fail
        n = self.stub.add(["agent:batou", "status:todo"])
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        row = self.ledger()[0]
        self.assertEqual((row["usage_before"], row["usage_after"], row["cost_usd"]), ({}, {}, None))

    def test_key_usage_is_empty_when_the_read_fails(self):
        for boom in (dispatch.Failure("no key"), ValueError("not JSON after all")):
            with self.subTest(boom=type(boom).__name__):
                with mock.patch.object(dispatch, "http", side_effect=boom):
                    self.assertEqual(dispatch.key_usage("http://x/api/v1", "k"), {})

    def test_a_failing_cpu_sample_still_runs_and_records_no_cpu(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatch, "cpu_sample", side_effect=OSError("no /proc/stat")):
            code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        row = self.ledger()[0]
        self.assertEqual((row["cpu"], row["cpu_before"], row["cpu_after"], row["outcome"]),
                         (None, None, None, "ok"))

    def test_the_ledger_file_is_not_world_readable(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        mode = (self.t / "state" / "runs.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)

    def test_the_ledger_writer_fixes_an_existing_files_mode(self):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"start": "old"}\n')
        path.chmod(0o644)                       # an older build (or a hand edit) left it world-readable
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(path.read_text().splitlines()[0], '{"start": "old"}')   # and it appended, not rewrote


class TestCpuUnits(unittest.TestCase):
    def test_proc_stat_ticks_are_converted_to_seconds(self):
        fields = [100, 0, 50, 900, 20, 10, 10, 5]        # user nice system idle iowait irq softirq steal
        with mock.patch.object(dispatch, "CLK_TCK", 100.0):
            self.assertAlmostEqual(dispatch.busy_seconds(fields), 1.7)   # (100+50+10+10)/100
            self.assertEqual(dispatch.cpu_sample.__name__, "cpu_sample")

    def test_the_draw_is_not_a_hundred_times_too_big(self):
        before = {"at": 0.0, "busy": 0.0, "steal": 0.0, "total": 0.0}
        after = {"at": 3600.0, "busy": 1185.0, "steal": 18.0, "total": 3600.0}
        d = dispatch.cpu_delta(before, after)
        self.assertAlmostEqual(d["credits"], 285.0)      # 1185 - 25% baseline x 3600 = 285
        self.assertAlmostEqual(d["steal_pct"], 0.5)


class TestPace(unittest.TestCase):
    def test_the_two_lines_for_a_forty_dollar_month(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        p = dispatch.month_pace(now, 40, 20)
        self.assertEqual(p["days"], 30)
        self.assertAlmostEqual(p["linear_per_day"], 39 / 30)
        self.assertAlmostEqual(p["linear_to_date"], (39 / 30) * 14.5)
        self.assertAlmostEqual(p["remaining_per_day"], (40 - 20 - 1) / 15.5)
        self.assertFalse(dispatch.over_pace(now, 40, 20))
        self.assertTrue(dispatch.over_pace(now, 40, 30))

    def test_slack_days_is_the_tolerance(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        pace = dispatch.month_pace(now, 40, 20)["linear_to_date"]
        just_over = pace + 0.5 * dispatch.month_pace(now, 40, 20)["linear_per_day"]
        self.assertFalse(dispatch.over_pace(now, 40, just_over, slack_days=1.0))
        self.assertTrue(dispatch.over_pace(now, 40, just_over, slack_days=0.0))


class TestCash(unittest.TestCase):
    def test_the_multiplier_defaults_to_one_and_reads_a_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "limits.toml"
            self.assertEqual(dispatch.cash_multiplier(p), 1.0)          # absent file
            p.write_text("cash_multiplier = 1.32\n")
            self.assertEqual(dispatch.cash_multiplier(p), 1.32)
            p.write_text("cash_multiplier = \"lots\"\n")                # not a number
            self.assertEqual(dispatch.cash_multiplier(p), 1.0)
            p.write_text("lanes = 2\n")
            self.assertEqual(dispatch.cash_multiplier(p), 1.0)


class TestLimitsExample(unittest.TestCase):
    """The two follow-ups meet in /etc/section9/limits.toml: #100's top-level cash_multiplier and #101's
    [models] and [spend] tables. A top-level key placed after a table would silently become part of it."""

    def runbook_limits(self):
        text = (Path(__file__).resolve().parent.parent / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        marker = "# /etc/section9/limits.toml"
        start = text.index(marker)
        return dispatch.tomllib.loads(text[start:text.index("```", start)])

    def test_the_runbook_example_parses_with_every_key_where_the_dispatcher_reads_it(self):
        data = self.runbook_limits()
        self.assertEqual(data["cash_multiplier"], 1.266)             # top level, not inside [timeout_sec]
        self.assertNotIn("cash_multiplier", data["timeout_sec"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "limits.toml"
            p.write_text((Path(__file__).resolve().parent.parent / "docs" / "RUNBOOK.md").read_text(
                encoding="utf-8").split("# /etc/section9/limits.toml", 1)[1].split("```", 1)[0].split("\n", 1)[1])
            self.assertEqual(dispatch.cash_multiplier(p), 1.266)
            self.assertEqual(dispatch.spend_caps(p), {"Saito": 8.0, "Tachikoma": 5.0})
            pins, allow = dispatch.models_of(p)
            self.assertIn("anthropic/claude-sonnet-5.5", allow)
            self.assertEqual(pins, {})

    def test_the_allow_list_in_the_example_holds_no_xai_model(self):
        self.assertTrue(all(dispatch.model_ok(m) for m in self.runbook_limits()["models"]["allow"]))


class TestPacing(Base):
    def limits(self, body):
        path = self.t / "limits.toml"
        path.write_text(body)
        return str(path)

    def test_off_by_default_and_a_low_priority_task_still_runs(self):
        n = self.stub.add(["agent:batou", "status:todo", "priority:low"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review", "priority:low"})

    def test_over_pace_holds_only_low_priority_work(self):
        self.stub.add(["agent:batou", "status:todo", "priority:low"])
        normal = self.stub.add(["agent:ishikawa", "status:todo"])
        with mock.patch.object(dispatch, "month_pace",
                               return_value={"linear_to_date": 0.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatch, "cpu_delta", return_value={"steal_pct": 0.0}):
            code, out = self.run_dispatch(limits=self.limits(
                "[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n"))
        low = next(i for i in self.stub.issues.values() if "priority:low" in self.stub.labels(i["number"]))
        self.assertEqual(code, 0)
        self.assertIn("status:todo", self.stub.labels(low["number"]))
        self.assertEqual(self.stub.labels(normal), {"agent:ishikawa", "status:review"})
        self.assertIn("holding 1 low-priority", out)

    def test_over_pace_holds_a_low_priority_task_whatever_the_label_case(self):
        # The pick lowercases labels; the pacing hold must normalise the same way (was a raw compare).
        low = self.stub.add(["agent:batou", "status:todo", "priority:Low"])
        normal = self.stub.add(["agent:ishikawa", "status:todo"])
        with mock.patch.object(dispatch, "month_pace",
                               return_value={"linear_to_date": 0.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatch, "cpu_delta", return_value={"steal_pct": 0.0}):
            code, out = self.run_dispatch(limits=self.limits(
                "[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n"))
        self.assertEqual(code, 0)
        self.assertIn("status:todo", self.stub.labels(low))
        self.assertEqual(self.stub.labels(normal), {"agent:ishikawa", "status:review"})
        self.assertIn("holding 1 low-priority", out)

    def test_an_index_error_reading_the_cpu_holds_low_priority_not_crashes(self):
        # #78 nit 1: a short /proc/stat line (IndexError) must fail closed, not crash the tick.
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"])
        normal = self.stub.add(["agent:ishikawa", "status:todo"])
        with mock.patch.object(dispatch, "month_pace",
                               return_value={"linear_to_date": 0.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatch, "cpu_sample", side_effect=IndexError("short cpu line")):
            code, out = self.run_dispatch(limits=self.limits(
                "[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n"))
        self.assertEqual(code, 0)
        self.assertIn("status:todo", self.stub.labels(low))
        self.assertEqual(self.stub.labels(normal), {"agent:ishikawa", "status:review"})
        self.assertIn("CPU figures unreadable", out)

    def test_under_pace_admits_the_low_priority_task_too(self):
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"])
        with mock.patch.object(dispatch, "month_pace",
                               return_value={"linear_to_date": 100.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatch, "cpu_delta", return_value={"steal_pct": 0.0}):
            self.run_dispatch(limits=self.limits(
                "[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n"))
        self.assertEqual(self.stub.labels(low), {"agent:batou", "status:review", "priority:low"})


class TestCapacity(Base):
    def capacity(self, limits=None):
        code, out = self.run_dispatch("capacity", limits=limits)
        self.assertEqual(code, 0, out)
        return out

    def test_the_cash_multiplier_shows_both_usage_and_cash(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("cash_multiplier = 1.32\n")
        self.stub.bump = 0.03
        self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.run_dispatch()
        self.capacity(limits=limits)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("$5.03", body)            # usage, as OpenRouter reports it
        self.assertIn("$6.64", body)            # cash = 5.03 x 1.32
        self.assertIn("Cash (x1.32", body)

    def test_without_the_multiplier_there_is_no_cash_line(self):
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertNotIn("Cash (x", body)

    def test_creates_then_updates_one_pinned_issue_with_the_roll_up(self):
        self.stub.bump = 0.03
        self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.run_dispatch()
        self.capacity()
        (n, issue), = [x for x in self.stub.issues.items() if x[1]["title"] == "Capacity"]
        self.assertEqual((self.stub.labels(n), self.stub.pinned), (set(), f"node-{n}"))
        for line in ("**Pace:**", "Last 7 days", "Month to date", "by agent", "by task type",
                     "Queue idle", "CPU credits", "$0.03", "| Batou | 1 |"):
            self.assertIn(line, issue["body"])
        self.capacity()
        self.assertEqual(sum(i["title"] == "Capacity" for i in self.stub.issues.values()), 1)

    def test_an_empty_ledger_still_writes_the_issue_and_says_what_is_missing(self):
        self.capacity()
        (n, issue), = self.stub.issues.items()
        self.assertIn("No ledger data yet", issue["body"])
        for line in ("—", "no run had a readable cost delta"):
            self.assertIn(line, issue["body"])

    def test_runs_without_a_cost_are_not_averaged_into_a_fake_rate(self):
        self.capacity()                       # creates the issue and the shape
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "v": 1, "issue": 1, "type": "qa", "agent": "Batou", "role": "engineer",
            "model": "hermes-agent", "start": "2026-11-04T10:00:00+00:00",
            "end": "2026-11-04T10:30:00+00:00", "seconds": 1800, "outcome": "ok",
            "queue_before": 2, "usage_before": {}, "usage_after": {}, "cost_usd": None,
            "tokens": None, "cpu": {"credits": 100}}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("no run had a readable cost delta", body)
        self.assertNotIn("$0.00/h", body)

    def test_capacity_shows_the_key_month_to_date_beside_the_ledger(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "v": 1, "issue": 1, "type": "qa", "agent": "Batou", "role": "engineer",
            "model": "deepseek/deepseek-v4.1-flash", "start": now, "end": now, "seconds": 600,
            "outcome": "ok", "queue_before": 1, "cost_usd": 0.0,       # the counter did not move
            "tokens": {"prompt_tokens": 0, "completion_tokens": 1_000_000},
            "cpu": {"credits": 1}}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("Key (OpenRouter `/key`), month to date: $5.00", body)   # the stub's usage_monthly
        self.assertIn("ledger books $0.19 over 1 run(s) (1 priced)", body)
        self.assertIn("$4.81 unattached to any run", body)
        self.assertNotIn("no cost figure", body)      # the $0 delta was priced from the run's tokens

    def test_a_corrupt_ledger_line_is_skipped_not_fatal(self):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n" + json.dumps({"start": "2026-11-04T10:00:00+00:00", "end":
            "2026-11-04T10:10:00+00:00", "agent": "Batou", "type": "qa", "seconds": 600,
            "outcome": "ok", "cost_usd": 0.02, "cpu": {"credits": 10}, "queue_before": 1}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("Runs: 1", body)


class TestDashboard(unittest.TestCase):
    def test_the_vendored_board_is_the_real_waits_page(self):
        # ghost-in-the-machine hq/board/index.html at commit 5643f3b (PR #18), vendored under
        # vps/board/. Its own build.sh checks these; the same checks here stop the copy drifting.
        page = (ROOT / "vps" / "board" / "index.html").read_text(encoding="utf-8")
        sample = json.loads((ROOT / "vps" / "board" / "board-sample.json").read_text(encoding="utf-8"))
        m = re.search(r'<script type="application/json" id="board-sample">(.*?)</script>', page, re.S)
        self.assertIsNotNone(m, "index.html has no inline #board-sample block")
        self.assertEqual(json.loads(m.group(1)), sample)          # the offline copy has not drifted
        self.assertEqual(sample.get("sample"), True)
        self.assertEqual(set(sample) - {"sample"}, {"generatedAt", "budget", "model", "agents", "tasks"})
        for needle in ("fetch('./board.json'", "POLL_MS = 60000", "START_LIMIT = 3", "prefers-reduced-motion",
                       "max-width:700px", "prefers-color-scheme", 'class="column waits-', 'class="pr"',
                       'id="lanes"', 'id="start-here"', 'id="chips"', 'id="columns"', 'id="filter-line"'):
            self.assertIn(needle, page, needle)

    def test_the_vendored_files_match_the_hashes_recorded_beside_them(self):
        import hashlib
        readme = (ROOT / "vps" / "board" / "README.md").read_text(encoding="utf-8")
        for name in ("index.html", "board-sample.json"):
            want = re.search(r"`%s`\s+sha256 `([0-9a-f]{64})`" % re.escape(name), readme)
            self.assertIsNotNone(want, name)
            have = hashlib.sha256((ROOT / "vps" / "board" / name).read_bytes()).hexdigest()
            self.assertEqual(have, want.group(1), f"{name} changed without its README hash")

    def test_the_placeholder_page_is_kept_and_reads_status_json(self):
        page = (ROOT / "vps" / "board" / "placeholder.html").read_text(encoding="utf-8")
        self.assertIn("/board/status.json", page)                 # the fallback's own feed

    def test_compose_enables_the_dashboard_on_loopback_with_themes_read_only(self):
        text = (ROOT / "vps" / "compose.yaml").read_text()
        for line in ('HERMES_DASHBOARD: "1"', 'HERMES_DASHBOARD_HOST: "127.0.0.1"', 'HERMES_DASHBOARD_PORT: "9119"',
                     "/var/lib/section9/dashboard-themes:/opt/data/dashboard-themes:ro"):
            self.assertIn(line, text)
        self.assertNotIn("9119:", text)                      # never published; host networking, bound to 127.0.0.1

    def test_config_has_the_documented_dashboard_keys(self):
        text = (ROOT / "hermes" / "config.yaml").read_text()
        self.assertRegex(text, r"(?m)^dashboard:\n  public_url: https://hq\.sectionnine\.dev$")
        self.assertIn('trusted_proxies: ["127.0.0.1"]', text)
        self.assertNotIn("0.0.0.0", text)
        self.assertRegex(text, r"(?m)^  theme: ghost$")

    def test_login_is_demanded_because_public_url_is_set_not_because_of_the_bind(self):
        # Hermes v0.22.5 (hermes_cli/web_server.py should_require_dashboard_auth) engages the auth gate for a
        # non-loopback bind OR a non-loopback dashboard.public_url. We bind 127.0.0.1 under host networking,
        # so public_url is the only thing making the generated login mandatory: it must stay set.
        cfg = (ROOT / "hermes" / "config.yaml").read_text()
        self.assertRegex(cfg, r"(?m)^  public_url: https://[a-z0-9.-]+$")
        self.assertNotIn("HERMES_DASHBOARD_PUBLIC_URL", (ROOT / "vps" / "compose.yaml").read_text())
        code = "\n".join(l for l in (ROOT / "vps" / "compose.yaml").read_text().splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("0.0.0.0", code)                    # host networking: it would listen on every interface

    def test_prep_makes_the_login_once_and_never_prints_it(self):
        text = (ROOT / "vps" / "dashboard-prep.sh").read_text()
        for k in ("USERNAME", "PASSWORD", "SECRET"):
            self.assertIn("HERMES_DASHBOARD_BASIC_AUTH_$k", text)
        self.assertEqual(text.count("openssl rand"), 2)
        self.assertIn("openssl rand -base64 32", text)       # the signing secret: 32 bytes, as Hermes's docs ask
        for line in text.splitlines():                       # the only line with the key writes to the file
            if "openssl rand" in line:
                self.assertTrue(line.rstrip().endswith('>> "$secrets/hermes.env"'), line)

    def test_prep_runs_before_compose_in_install_and_sync(self):
        for name, anchor in (("install.sh", "docker compose -f"), ("sync.sh", "docker compose -f")):
            text = (ROOT / "vps" / name).read_text()
            self.assertIn("dashboard-prep.sh", text, name)
            self.assertLess(text.index("dashboard-prep.sh"), text.index(anchor), name)

    def test_prep_script_run_for_real(self):
        with tempfile.TemporaryDirectory() as d:
            sec, state = Path(d, "sec"), Path(d, "state")
            sec.mkdir()
            (sec / "hermes.env").write_text("OPENROUTER_API_KEY=abc")          # no trailing newline
            env = {**os.environ, "S9_SECRETS_DIR": str(sec), "S9_STATE_DIR": str(state)}
            run = lambda: subprocess.run([str(ROOT / "vps" / "dashboard-prep.sh")], env=env, capture_output=True, text=True, check=True)
            first = run()
            lines = (sec / "hermes.env").read_text().splitlines()
            self.assertEqual(lines[0], "OPENROUTER_API_KEY=abc")
            made = dict(l.split("=", 1) for l in lines[1:])
            self.assertEqual(set(made), {f"HERMES_DASHBOARD_BASIC_AUTH_{k}" for k in ("USERNAME", "PASSWORD", "SECRET")})
            self.assertEqual(len(made["HERMES_DASHBOARD_BASIC_AUTH_USERNAME"]), 32)
            self.assertEqual(len(made["HERMES_DASHBOARD_BASIC_AUTH_PASSWORD"]), 32)
            self.assertEqual(len(base64.b64decode(made["HERMES_DASHBOARD_BASIC_AUTH_SECRET"])), 32)
            for v in made.values():
                self.assertNotIn(v, first.stdout + first.stderr)
            self.assertIn("sudo grep DASHBOARD", first.stdout)
            (state / "dashboard-themes" / "ghost.yaml").write_text("name: mine\n")
            second = run()                                                       # nothing changes, nothing printed
            self.assertEqual((sec / "hermes.env").read_text().splitlines()[1:], [f"{k}={v}" for k, v in made.items()])
            self.assertEqual(second.stdout, "")
            self.assertEqual((state / "dashboard-themes" / "ghost.yaml").read_text(), "name: mine\n")
            (state / "dashboard-themes" / "ghost.yaml").unlink()
            run()
            self.assertIn("name: ghost", (state / "dashboard-themes" / "ghost.yaml").read_text())

    def test_check_in_has_no_paperclip_and_runs_once_at_eight(self):
        text = (ROOT / "docs" / "check-in.md").read_text()
        self.assertNotIn("aperclip", text)
        self.assertIn("CRON_TZ=Europe/Stockholm 0 8 * * *", text)
        for repo in ("flight-tracker", "ghost-in-the-machine", "agent-manager", "section9-hq"):
            self.assertIn(f"repo {repo}", text)
        self.assertNotIn("Midday", text)


class TestAnyAgent(Base):
    """agent:any and agent:<role>: the pool takes the task, never its author, least recent work first."""

    ROSTER = """
[[agents]]
name = "Batou"
brief = "agents/coder.md"
role = "engineer"
timeout_sec = 60

[[agents]]
name = "Ishikawa"
brief = "agents/researcher.md"
role = "researcher"
timeout_sec = 60

[[agents]]
name = "Togusa"
brief = "agents/backup-coordinator.md"
role = "general"
title = "QA and UI/UX reviewer"
timeout_sec = 60

[[agents]]
name = "Saito"
brief = "agents/ops.md"
role = "devops"
timeout_sec = 60
"""

    def setUp(self):
        super().setUp()
        (self.t / "roster.toml").write_text(self.ROSTER)

    def worked(self, labels, stamp):
        n = self.stub.add(labels)
        self.stub.issues[n]["updated_at"] = stamp
        return n

    def system_and_user(self):
        body, auth, session = self.stub.hermes_calls[0]
        return body["messages"][0]["content"], body["messages"][1]["content"], session

    def test_any_takes_the_least_recently_active_agent_not_the_author(self):
        self.worked(["agent:batou", "status:done"], "2026-11-03T12:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T11:00:00Z")
        self.worked(["agent:togusa", "status:done"], "2026-11-03T10:00:00Z")
        self.worked(["agent:saito", "status:done"], "2026-11-03T09:00:00Z")
        n = self.stub.add(["agent:any", "status:todo", "author:batou"], "QA Batou's PR")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        system, user, session = self.system_and_user()
        self.assertIn("You are **Saito**", system)            # oldest activity, and not the author
        self.assertNotIn("You are **Batou**", system)
        self.assertIn(f"s9-task --as Saito status {n}", user)
        self.assertEqual(session, f"issue-{n}")
        self.assertEqual(self.stub.labels(n), {"agent:saito", "author:batou", "status:review"})

    def test_any_skips_an_author_who_would_otherwise_be_oldest(self):
        self.worked(["agent:saito", "status:done"], "2026-11-03T12:00:00Z")
        self.worked(["agent:togusa", "status:done"], "2026-11-03T11:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T10:00:00Z")
        self.worked(["agent:batou", "status:done"], "2026-11-03T08:00:00Z")   # oldest, but wrote it
        n = self.stub.add(["agent:any", "status:todo", "author:batou"], "Review Batou's work")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        system = self.system_and_user()[0]
        self.assertIn("You are **Ishikawa**", system)
        self.assertNotIn("You are **Batou**", system)
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "author:batou", "status:review"})

    def test_an_author_named_in_the_body_is_also_excluded(self):
        n = self.stub.add(["agent:any", "status:todo"], "QA the PR")
        self.stub.issues[n]["body"] = "Author: Batou\n\nPlease review."
        self.run_dispatch()
        system = self.system_and_user()[0]
        self.assertNotIn("You are **Batou**", system)

    def test_a_named_agent_who_wrote_it_is_blocked_not_run(self):
        n = self.stub.add(["agent:batou", "status:todo", "author:batou"], "Review Batou's PR")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "author:batou", "status:blocked"})
        self.assertIn("wrote it", self.stub.comments[0][1])
        self.assertIn("status:todo", self.stub.comments[0][1])

    def test_a_role_label_reaches_only_an_agent_with_that_role(self):
        n = self.stub.add(["agent:engineer", "status:todo", "author:ishikawa"], "Fix the bug")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("You are **Batou**", self.system_and_user()[0])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "author:ishikawa", "status:review"})

    def test_a_role_with_only_its_author_blocks_instead_of_self_reviewing(self):
        n = self.stub.add(["agent:qa", "status:todo", "author:togusa"], "QA Togusa's own PR")
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:qa", "author:togusa", "status:blocked"})
        self.assertIn("the author (togusa)", self.stub.comments[0][1])

    def test_oldest_any_task_still_goes_first(self):
        first = self.stub.add(["agent:any", "status:todo"], "First")
        second = self.stub.add(["agent:any", "status:todo"], "Second")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("First", self.system_and_user()[1])
        self.assertEqual(self.stub.labels(first), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(second), {"agent:any", "status:todo"})

    def test_a_named_task_is_still_taken_by_its_named_agent(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"])
        self.run_dispatch()
        self.assertIn("You are **Ishikawa**", self.system_and_user()[0])
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:review"})

    def test_pooled_task_is_recorded_in_the_ledger_under_the_agent_it_went_to(self):
        self.worked(["agent:batou", "status:done"], "2026-11-03T12:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T11:00:00Z")
        self.worked(["agent:togusa", "status:done"], "2026-11-03T10:00:00Z")
        self.worked(["agent:saito", "status:done"], "2026-11-03T09:00:00Z")
        n = self.stub.add(["agent:any", "status:todo"])
        self.run_dispatch()
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual((row["issue"], row["agent"], row["outcome"]), (n, "Saito", "ok"))

    def test_pacing_holds_a_low_priority_pooled_task_and_runs_the_next_one(self):
        low = self.stub.add(["agent:any", "status:todo", "priority:low"])
        normal = self.stub.add(["agent:ishikawa", "status:todo"])
        path = self.t / "limits.toml"
        path.write_text("[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n")
        with mock.patch.object(dispatch, "month_pace",
                               return_value={"linear_to_date": 0.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatch, "cpu_delta", return_value={"steal_pct": 0.0}):
            self.run_dispatch(limits=str(path))
        self.assertIn("status:todo", self.stub.labels(low))
        self.assertIn("agent:any", self.stub.labels(low))
        self.assertEqual(self.stub.labels(normal), {"agent:ishikawa", "status:review"})


class TestPriority(Base):
    def test_a_high_priority_bug_is_picked_before_an_older_ordinary_task(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old feature request")
        bug = self.stub.add(["agent:batou", "status:todo", "priority:high"], "Fix the broken board")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("Fix the broken board", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(bug), {"agent:batou", "priority:high", "status:review"})
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:todo"})

    def test_a_low_priority_task_waits_behind_an_older_ordinary_one(self):
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"], "Nice to have, older")
        normal = self.stub.add(["agent:batou", "status:todo"], "Ordinary, newer")
        self.run_dispatch()
        self.assertIn("Ordinary, newer", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(normal), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(low), {"agent:batou", "priority:low", "status:todo"})

    def test_without_priority_labels_the_oldest_is_still_picked(self):
        self.stub.add(["agent:batou", "status:todo"], "First")
        self.stub.add(["agent:batou", "status:todo"], "Second")
        self.run_dispatch()
        self.assertIn("First", self.stub.hermes_calls[0][0]["messages"][1]["content"])

    def test_priority_picks_the_issue_and_agent_any_still_picks_the_agent(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old ordinary")
        bug = self.stub.add(["agent:any", "status:todo", "priority:high"], "New high bug")
        self.run_dispatch()
        self.assertIn("New high bug", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        labels = self.stub.labels(bug)
        self.assertIn("status:review", labels)
        self.assertTrue(any(l.startswith("agent:") and l != "agent:any" for l in labels))
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:todo"})

    def test_an_issue_with_two_agent_labels_chooses_the_same_one_under_any_hash_seed(self):
        lines = [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location('d', sys.argv[1])",
            "m = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(m)",
            "issue = {'labels': [{'name': 'agent:any'}, {'name': 'agent:qa'}], 'body': ''}",
            "agents = [{'name': 'A'}, {'name': 'B', 'role': 'qa'}, {'name': 'C', 'title': 'QA reviewer'}]",
            "print(len(m.candidates(issue, agents)[0]))",
        ]
        seen = set()
        for seed in ("0", "3", "12345", "random"):        # seed 3 exposes the old set-order pick
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", "\n".join(lines), str(ROOT / "vps" / "dispatch.py")],
                                 capture_output=True, text=True, env=env)
            seen.add(out.stdout.strip())
        self.assertEqual(seen, {"3"})        # 'any' wins the tie, so all three agents, delete-proof

    def test_an_unknown_agent_label_is_blocked_when_it_is_the_only_todo(self):
        n = self.stub.add(["agent:ghost", "status:todo"])
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:ghost", "status:blocked"})
        self.assertIn("no agent has the role", self.stub.comments[0][1])

    def test_an_unknown_agent_label_is_left_alone_while_other_work_runs(self):
        bad = self.stub.add(["agent:ghost", "status:todo"])
        good = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(bad), {"agent:ghost", "status:todo"})
        self.assertEqual(self.stub.labels(good), {"agent:batou", "status:review"})

    def test_priority_rank_ages_a_waiting_task_up_one_level(self):
        # Direct, so the rule is pinned: a task over 12 h rises one level, a low one over 48 h tops out.
        now = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)
        def rank(labels, ago_h):
            issue = {"labels": [{"name": x} for x in labels],
                     "created_at": (now - dt.timedelta(hours=ago_h)).isoformat()}
            return dispatch.priority_rank(issue, now)
        self.assertEqual(rank([], 0), 1)
        self.assertEqual(rank(["priority:high"], 0), 0)
        self.assertEqual(rank(["priority:Low"], 0), 2)         # label case never matters
        self.assertEqual(rank(["Priority:HIGH"], 0), 0)
        self.assertEqual(rank([], 5), 1)                       # under the line: unchanged
        self.assertEqual(rank([], 13), 0)                      # an old ordinary joins the high group
        self.assertEqual(rank(["priority:low"], 13), 1)        # a low one rises one level
        self.assertEqual(rank(["priority:low"], 47), 1)
        self.assertEqual(rank(["priority:low"], 49), 0)        # over 48 h it tops out
        self.assertEqual(rank(["priority:high"], 49), 0)       # already at the top

    def test_a_starving_ordinary_task_overtakes_a_fresh_high_task(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old ordinary, waiting", age_min=13 * 60)
        bug = self.stub.add(["agent:batou", "status:todo", "priority:high"], "Fresh high bug")
        self.run_dispatch()
        self.assertIn("Old ordinary, waiting", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(bug), {"agent:batou", "priority:high", "status:todo"})

    def test_a_low_task_over_48h_overtakes_a_high_task(self):
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"], "Low, waiting 49 h", age_min=49 * 60)
        high = self.stub.add(["agent:batou", "status:todo", "priority:high"], "High, fresh")
        self.run_dispatch()
        self.assertIn("Low, waiting 49 h", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(low), {"agent:batou", "priority:low", "status:review"})
        self.assertEqual(self.stub.labels(high), {"agent:batou", "priority:high", "status:todo"})

class TestLanes(Base):
    """#79: the second lane -- the lanes switch, what it may run beside lane 0, and the fallback."""

    TWO = "lanes = 2\n[timeout_sec]\ndefault = 60\n[pacing]\nsample_seconds = 0\n"

    def limits(self, text):
        path = self.t / "limits.toml"
        path.write_text(text)
        return str(path)

    def ledger(self):
        return [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]

    def latch(self):
        return json.loads((self.t / "state" / "lanes.json").read_text())

    def seed(self, rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def lane_one(self, limits=None):
        """Run lane 1 with the steal sample stubbed out: the tests seed the rest of the gate."""
        with mock.patch.object(dispatch, "steal_secs", return_value=0.0):
            return self.run_dispatch("run", limits=limits or self.limits(self.TWO), lane=1)

    def test_lane_one_is_off_unless_the_switch_says_two(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        code, out = self.run_dispatch(lane=1)                      # no limits file at all: one lane
        self.assertEqual((code, self.stub.hermes_calls, out), (0, [], ""))   # quiet: no per-tick line while lanes = 1
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})
        code, out = self.run_dispatch(lane=1, limits=self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))

    def test_the_second_lane_runs_beside_lane_zero(self):
        busy = self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        mine = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        same_agent = self.stub.add(["agent:batou", "status:todo"], "portfolio: build direction B")
        code, out = self.lane_one()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("You are **Ishikawa**", self.stub.hermes_calls[0][0]["messages"][0]["content"])
        self.assertEqual(self.stub.labels(mine), {"agent:ishikawa", "status:review"})
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(same_agent), {"agent:batou", "status:todo"})
        self.assertEqual(self.stub.labels(busy), {"agent:batou", "status:working"})
        self.assertIn("(lane 1)", out)

    def test_the_second_lane_picks_by_priority_among_what_it_may_run(self):
        """#71 priority order and #79 lane filter together: the filter drops first, then priority picks."""
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        self.stub.add(["agent:batou", "status:todo", "priority:high"], "portfolio: urgent, but batou is busy")
        old = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: old ordinary")
        low = self.stub.add(["agent:saito", "status:todo", "priority:low"], "dinner-planner: nice to have")
        high = self.stub.add(["agent:saito", "status:todo", "priority:high"], "ghost-in-the-machine: broken")
        code, _ = self.lane_one()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(high), {"agent:saito", "status:review", "priority:high"})
        self.assertEqual(self.stub.labels(old), {"agent:ishikawa", "status:todo"})
        self.assertEqual(self.stub.labels(low), {"agent:saito", "status:todo", "priority:low"})
        self.assertEqual(self.ledger()[0]["lane"], 1)

    def test_the_second_lane_skips_a_task_whose_repo_it_cannot_tell(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        unknown = self.stub.add(["agent:ishikawa", "status:todo"], "Do a thing")
        known = self.stub.add(["agent:saito", "status:todo"], "nvim-trainer: add a keymap")
        code, _ = self.lane_one()
        self.assertEqual((code, self.stub.labels(unknown)), (0, {"agent:ishikawa", "status:todo"}))
        self.assertEqual(self.stub.labels(known), {"agent:saito", "status:review"})

    def test_an_unknown_repo_already_running_stands_the_second_lane_down(self):
        self.stub.add(["agent:batou", "status:working"], "Night build 2026-11-05 (Tachikoma)")
        self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        code, out = self.lane_one()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("found nothing", out)
        self.assertEqual(self.latch()["lanes"], 2)      # the reading was safe: two lanes stand

    def test_two_lanes_run_two_tasks_at_the_same_time(self):
        """Two real processes on the same state dir: lane 1 starts while lane 0 is in Hermes."""
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: fix the popping")
        second = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        limits = self.limits(self.TWO)
        self.stub.hermes = "slow-report"                # 2.5 s in Hermes: a real overlap
        def lane(n):
            argv = [sys.executable, str(ROOT / "vps" / "dispatch.py"), "run", "--lane", str(n),
                    "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                    "--roster", str(self.t / "roster.toml"), "--private-routines", str(self.t / "none.toml"),
                    "--limits", limits, "--github", self.url + "/gh", "--openrouter", self.url + "/or",
                    "--hermes", self.url + "/hermes"]
            return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        zero = lane(0)
        time.sleep(0.5)                                 # lane 0 is inside its Hermes call by now
        one = lane(1)
        out0, err0 = zero.communicate(timeout=60)
        out1, err1 = one.communicate(timeout=60)
        self.assertEqual((zero.returncode, one.returncode), (0, 0), err0 + err1)
        for text in (out0 + err0, out1 + err1):
            for secret in (GH, ORKEY, HKEY):
                self.assertNotIn(secret, text)
        self.assertEqual(sorted(session for _, _, session in self.stub.hermes_calls),
                         [f"issue-{first}", f"issue-{second}"])
        self.assertEqual([self.stub.labels(first), self.stub.labels(second)],
                         [{"agent:batou", "status:review"}, {"agent:ishikawa", "status:review"}])
        rows = self.ledger()
        self.assertEqual(sorted(r["lane"] for r in rows), [0, 1])
        self.assertGreater(dispatch.concurrent_hours(rows), 0.0)
        self.assertIn("(lane 1)", out1)

    def test_cpu_steal_drops_the_second_lane_and_writes_the_latch(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        with mock.patch.object(dispatch, "steal_secs", return_value=25.0):
            code, out = self.run_dispatch("run", limits=self.limits(self.TWO), lane=1)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:todo"})
        self.assertIn("down to one lane", out)
        self.assertIn("steal 25.0%", out)
        self.assertEqual(self.latch()["lanes"], 1)
        self.assertIn("Hostinger", self.latch()["reason"])

    def test_unreadable_cpu_stands_the_second_lane_down_cleanly(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        with mock.patch.object(dispatch, "steal_secs", side_effect=OSError("no /proc/stat")):
            code, out = self.run_dispatch("run", limits=self.limits(self.TWO), lane=1)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:todo"})
        self.assertIn("lanes: down to one lane: cpu unreadable (no /proc/stat)", out)
        self.assertEqual(self.latch()["lanes"], 1)

    def lane_zero(self, limits):
        with mock.patch.object(dispatch, "steal_secs", return_value=0.0):
            return self.run_dispatch("run", limits=limits, lane=0)

    def test_lane_zero_skips_the_agent_lane_one_is_running(self):
        self.stub.add(["agent:batou", "status:working"], "portfolio: build direction B")
        same_agent = self.stub.add(["agent:batou", "status:todo"], "nvim-trainer: add a keymap")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_agent), {"agent:batou", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_zero_skips_the_repo_lane_one_is_working_in(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_zero_skips_an_unknown_repo_task_while_lane_one_runs(self):
        # #78 should-fix 1: while the other lane is running anything, an unknown-repo task conflicts too.
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        unknown = self.stub.add(["agent:ishikawa", "status:todo"], "Do a thing")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(unknown), {"agent:ishikawa", "status:todo"})

    def test_lane_zero_keeps_the_exclusion_while_lane_one_holds_its_run_lock(self):
        # #78 should-fix 2: lanes flipped back to 1 mid-run must not open the gap beside a live lane-1 run.
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        (self.t / "state").mkdir(exist_ok=True)
        lock = open(self.t / "state" / "dispatch.lane1.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            code, _ = self.run_dispatch("run", limits=self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"), lane=0)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_one_stands_down_cleanly_on_an_index_error(self):
        # #78 nit 1: a short /proc/stat line must not crash the lane.
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        with mock.patch.object(dispatch, "cpu_sample", side_effect=IndexError("short cpu line")):
            code, out = self.run_dispatch("run", limits=self.limits(self.TWO), lane=1)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:todo"})
        self.assertIn("cpu unreadable", out)
        self.assertEqual(self.latch()["lanes"], 1)

    def test_lane_zero_is_unchanged_with_one_lane(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: another pop")
        code, _ = self.lane_zero(self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(first), {"agent:batou", "status:review"})

    def test_the_credit_estimate_over_the_daily_guidance_drops_the_second_lane(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        self.seed([{"start": at, "end": at, "agent": "Batou", "lane": 0, "cpu": {"credits": 9000}}])
        code, out = self.lane_one()
        self.assertEqual((code, self.stub.labels(n)), (0, {"agent:ishikawa", "status:todo"}))
        self.assertIn("9000 CPU credits", out)
        self.assertEqual(self.latch()["lanes"], 1)

    def test_the_second_lane_returns_only_after_the_quiet_period(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        limits = self.limits(self.TWO)
        latch = self.t / "state" / "lanes.json"
        latch.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda h: (now - dt.timedelta(hours=h)).isoformat(timespec="seconds")   # noqa: E731
        latch.write_text(json.dumps({"lanes": 1, "unsafe_at": stamp(0.5)}))
        code, out = self.lane_one(limits)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("quiet period", out)
        latch.write_text(json.dumps({"lanes": 1, "unsafe_at": stamp(4)}))
        code, out = self.lane_one(limits)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:review"})
        self.assertIn("back to two lanes", out)
        self.assertEqual(self.latch()["lanes"], 2)

    def test_the_ledger_records_the_lane(self):
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: fix the popping")
        self.assertEqual(self.run_dispatch()[0], 0)
        second = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        self.lane_one()
        self.assertEqual([(r["issue"], r["lane"]) for r in self.ledger()], [(first, 0), (second, 1)])

    def test_each_lane_has_its_own_run_lock(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        (self.t / "state").mkdir(exist_ok=True)
        with open(self.t / "state" / "dispatch.lane0.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, _ = self.lane_one()
        self.assertEqual((code, self.stub.labels(n)), (0, {"agent:ishikawa", "status:review"}))

    def test_one_lane_chooses_at_a_time(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        (self.t / "state").mkdir(exist_ok=True)
        with open(self.t / "state" / "dispatch.pick.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})
        self.assertIn("choosing", out)

    def test_capacity_reports_the_lanes(self):
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda h: (now - dt.timedelta(hours=h)).isoformat(timespec="seconds")   # noqa: E731
        self.seed([{"start": stamp(3), "end": stamp(2), "agent": "Batou", "type": "qa", "lane": 0,
                    "seconds": 3600, "outcome": "ok", "cost_usd": 0.05, "cpu": {"credits": 100},
                    "queue_before": 2},
                   {"start": stamp(2.5), "end": stamp(1.5), "agent": "Ishikawa", "type": "research", "lane": 1,
                    "seconds": 3600, "outcome": "ok", "cost_usd": 0.04, "cpu": {"credits": 90},
                    "queue_before": 3}])
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("lane 0: 1 run(s), 1.00 h", body)
        self.assertIn("lane 1: 1 run(s), 1.00 h", body)
        self.assertIn("two at once 0.50 h", body)

    def test_repo_key_and_the_lanes_switch(self):
        self.assertEqual(dispatch.repo_key({"title": "flight-tracker: x", "body": "", "labels": []}), "flight-tracker")
        self.assertEqual(dispatch.repo_key({"title": "QA the PR", "body": "see nvim-trainer", "labels": []}),
                         "nvim-trainer")
        self.assertEqual(dispatch.repo_key({"title": "Do a thing", "body": "", "labels": [{"name": "repo:portfolio"}]}),
                         "portfolio")
        self.assertEqual(dispatch.repo_key({"title": "Do a thing", "body": "", "labels": []}), "")
        path = self.t / "limits.toml"
        for text, want in (("", 1), ("lanes = 1\n", 1), ("lanes = 2\n", 2), ("lanes = 3\n", 1),
                           ("lanes = '2'\n", 1)):
            path.write_text(text)
            self.assertEqual(dispatch.lanes_setting(path), want, text)

    def test_the_two_lanes_never_overlap(self):
        rows = [{"start": "2026-11-05T10:00:00+00:00", "end": "2026-11-05T11:00:00+00:00", "lane": 0},
                {"start": "2026-11-05T10:30:00+00:00", "end": "2026-11-05T11:30:00+00:00", "lane": 1},
                {"start": "2026-11-05T12:00:00+00:00", "end": "2026-11-05T12:10:00+00:00", "lane": 0}]
        self.assertAlmostEqual(dispatch.concurrent_hours(rows), 0.5)
        self.assertTrue(dispatch.lane_line(rows).endswith("two at once 0.50 h"))
        self.assertEqual(dispatch.lane_line([]), "—")


class TestModels(Base):
    """#101: the per-agent model, the root-only allow-list and the per-agent spend guard."""

    def limits(self, body):
        path = self.t / "limits.toml"
        path.write_text(body)
        return str(path)

    def seed(self, rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def body_of(self, index=0):
        return self.stub.hermes_calls[index][0]

    def test_a_roster_model_in_the_allow_list_is_sent_with_the_provider(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual(body["model"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(body["provider"], "openrouter")

    def test_a_roster_model_outside_the_allow_list_falls_back_to_the_default(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["deepseek/deepseek-v4.1-flash"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        code, out = self.run_dispatch(limits=limits)
        self.assertEqual((code, self.body_of()["model"]), (0, "hermes-agent"))
        self.assertIn("not in the root allow-list", out)

    def test_without_an_allow_list_the_roster_model_is_not_used(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch()                     # no limits file at all
        self.assertEqual(self.body_of()["model"], "hermes-agent")

    def test_the_default_request_is_the_model_the_messages_and_the_stream_flag(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()                     # no [models] table, roster names no model
        body = self.body_of()
        self.assertEqual(set(body), {"model", "messages", "stream"})
        self.assertEqual(body["model"], "hermes-agent")
        self.assertTrue(body["stream"])         # read as SSE, so a timeout keeps the partial answer
        self.assertNotIn("provider", body)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_a_default_resolved_model_also_streams(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\nSaito = "deepseek/deepseek-v4.1-flash"\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual((set(body), body["model"]), ({"model", "messages", "stream"}, "hermes-agent"))

    def test_the_saito_override_request_body_is_exact(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual(set(body), {"model", "provider", "messages", "stream"})
        self.assertEqual((body["model"], body["provider"]), ("anthropic/claude-sonnet-5.5", "openrouter"))

    def test_an_xai_model_is_refused_even_if_the_allow_list_names_it(self):
        pins, allow = dispatch.models_of(self.limits(
            '[models]\nallow = ["x-ai/grok-9-turbo"]\n'))
        self.assertEqual(dispatch.model_for({"name": "Saito", "model": "x-ai/grok-9-turbo"}, pins, allow),
                         dispatch.DEFAULT_MODEL)

    def test_the_root_pin_rolls_the_agent_back_with_one_line(self):
        pins, allow = dispatch.models_of(self.limits(
            '[models]\nallow = ["deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5"]\n'
            'Saito = "deepseek/deepseek-v4.1-flash"\n'))
        self.assertEqual(dispatch.model_for({"name": "Saito", "model": "anthropic/claude-sonnet-5.5"}, pins, allow),
                         dispatch.DEFAULT_MODEL)

    def test_the_ledger_records_the_model_that_answered(self):
        self.stub.hermes_runtime = {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
                                    "route_source": "raw_request"}
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n'))
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(row["model"], "deepseek/deepseek-v4.1-flash")       # the served model, not the asked one
        self.assertEqual(row["model_requested"], "anthropic/claude-sonnet-5.5")

    def test_the_ledger_falls_back_to_the_reply_model_when_no_runtime_is_reported(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n'))
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(row["model"], row["model_requested"])

    def test_the_agent_is_blocked_when_its_monthly_budget_is_reached(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5", "cost_usd": 6.5}])
        n = self.stub.add(["agent:saito", "status:todo"])
        code, out = self.run_dispatch(limits=self.limits("[spend]\nSaito = 6.0\n"))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:blocked"})
        self.assertIn("monthly budget", self.stub.comments[0][1])
        self.assertIn("$6.50", self.stub.comments[0][1])

    def test_a_run_under_the_budget_still_goes(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5", "cost_usd": 1.0}])
        n = self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits("[spend]\nSaito = 6.0\n"))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:review"})

    def test_an_agent_without_a_spend_entry_has_no_guard(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "cost_usd": 99.0}])
        n = self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits("[spend]\nTachikoma = 6.0\n"))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:review"})

    def test_the_guard_estimates_a_run_the_counter_could_not_price(self):
        self.assertAlmostEqual(dispatch.run_cost({"model": "anthropic/claude-sonnet-5.5", "cost_usd": None,
                                                  "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}),
                               0.012)
        self.assertEqual(dispatch.run_cost({"model": "who/knows", "tokens": {"completion_tokens": 1_000_000}}),
                         10.0)              # an unknown model is priced at the dearest allowed rate

    def test_capacity_shows_cost_by_agent_and_model(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "end": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5",
                    "type": "review", "seconds": 600, "outcome": "ok", "cost_usd": 0.2, "cpu": {"credits": 10},
                    "queue_before": 1}])
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("by agent and model", body)
        self.assertIn("Saito \u00b7 anthropic/claude-sonnet-5.5", body)


class TestRunCost(unittest.TestCase):
    """#205 item 1: a $0 key delta is not a free run; price it from the run's tokens."""

    def test_a_zero_key_delta_is_priced_from_the_run_tokens(self):
        row = {"model": "deepseek/deepseek-v4.1-flash", "cost_usd": 0.0,
               "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}
        self.assertAlmostEqual(dispatch.run_cost(row), (1000 * 0.05 + 1000 * 0.19) / 1_000_000.0)

    def test_a_measured_delta_still_wins_over_the_tokens(self):
        self.assertEqual(dispatch.run_cost({"model": "deepseek/deepseek-v4.1-flash", "cost_usd": 0.25,
                                            "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}),
                         0.25)

    def test_a_zero_delta_with_no_tokens_stays_zero_and_unpriced(self):
        self.assertEqual(dispatch.run_cost({"cost_usd": 0.0, "tokens": None}), 0.0)
        self.assertFalse(dispatch.priceable({"cost_usd": 0.0, "tokens": None}))
        self.assertTrue(dispatch.priceable({"cost_usd": 0.0, "tokens": {"total_tokens": 1}}))


class TestRosterTimeouts(unittest.TestCase):
    """#205 item 2: Batou and Togusa time out at 40 min, shipped as the roster default."""

    def roster(self):
        return dispatch.tomllib.loads((ROOT / "agents" / "roster.toml").read_text(encoding="utf-8"))

    def test_batou_and_togusa_default_to_2400_seconds(self):
        timeouts = {a["name"]: a["timeout_sec"] for a in self.roster()["agents"]}
        self.assertEqual((timeouts["Batou"], timeouts["Togusa"]), (2400, 2400))

    def test_the_night_build_keeps_its_longer_run(self):
        timeouts = {a["name"]: a["timeout_sec"] for a in self.roster()["agents"]}
        self.assertEqual(timeouts["Tachikoma"], 10800)     # only the two are lowered


class HealthRun(unittest.TestCase):
    """scripts/health-run.sh: the table it renders, its redaction, and what it posts.

    That script is bash and runs on the VPS inside the sandbox image, so it is
    driven here as a subprocess: a directory of empty checkouts, a stub `gh` first
    on PATH, and `--cmd` overrides. No clone, no network, no real issue.
    """

    SCRIPT = ROOT / "scripts" / "health-run.sh"
    REPOS = ("ghost-in-the-machine", "nvim-trainer", "portfolio", "dinner-planner", "flight-tracker")
    TOKEN = "github_pat_" + "t" * 22

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.repos = root / "repos"
        for r in self.REPOS:
            (self.repos / r).mkdir(parents=True)
        self.bin = root / "bin"
        self.bin.mkdir()
        self.gh_log, self.gh_body = root / "gh.log", root / "gh-body"
        (self.bin / "gh").write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$GH_LOG\"\n"
            "for a in \"$@\"; do case \"$a\" in body=@*) cp \"${a#body=@}\" \"$GH_BODY\";; esac; done\n"
            "case \"$*\" in\n"
            "  *graphql*) echo '{\"data\":{\"pinIssue\":{\"issue\":{\"number\":7}}}}' ;;\n"
            "  *'-f title='*) echo '{\"number\":7,\"node_id\":\"N1\"}' ;;\n"
            "esac\n"
            "exit 0\n")
        (self.bin / "gh").chmod(0o755)
        self.env = dict(os.environ, GH_TOKEN=self.TOKEN, GH_LOG=str(self.gh_log),
                        GH_BODY=str(self.gh_body), PATH=f"{self.bin}:{os.environ['PATH']}")

    def run_health(self, *args):
        return subprocess.run([str(self.SCRIPT), "--repos-dir", str(self.repos), *args],
                              capture_output=True, text=True, env=self.env, timeout=120)

    def cmds(self, overrides=None):
        """--cmd for every repo: `true`, or the override given for that repo."""
        overrides = overrides or {}
        args = []
        for r in self.REPOS:
            args += ["--cmd", f"{r}={overrides.get(r, 'true')}"]
        return args

    def test_dry_run_lists_every_repo_and_posts_nothing(self):
        out = self.run_health("--dry-run", *self.cmds())
        self.assertEqual(out.returncode, 0, out.stderr)
        for r in self.REPOS:
            self.assertIn(f"| {r} | PASS |", out.stdout)
        self.assertIn("**As of ", out.stdout)
        self.assertFalse(self.gh_log.exists(), "a dry run must not call gh")

    def test_a_failure_shows_fail_with_the_tail_and_no_token(self):
        boom = "echo keep-0; for i in $(seq 1 30); do echo keep-$i; done; " \
               "echo sk-or-v1-abcdefghijklmnopqrstuvwxyz; exit 1"
        out = self.run_health("--dry-run", *self.cmds({"nvim-trainer": boom}))
        self.assertEqual(out.returncode, 1)                     # a FAIL is a non-zero exit
        self.assertIn("| nvim-trainer | FAIL |", out.stdout)
        self.assertIn("<details><summary>nvim-trainer", out.stdout)
        self.assertIn("keep-30", out.stdout)                    # the tail is there
        self.assertNotIn("keep-5", out.stdout)                  # and it is only the tail
        self.assertNotIn("sk-or-v1-abcdefghijklmnopqrstuvwxyz", out.stdout)
        self.assertIn("[redacted]", out.stdout)
        self.assertNotIn("ghost-in-the-machine | FAIL", out.stdout)

    def test_a_run_creates_and_pins_the_issue_with_the_table(self):
        out = self.run_health(*self.cmds())
        self.assertEqual(out.returncode, 0, out.stderr)
        calls = self.gh_log.read_text()
        self.assertIn("issues?state=open", calls)                # looked for it first
        self.assertIn("-f title=Health run", calls)              # created it
        self.assertIn("graphql", calls)                          # and asked to pin it
        body = self.gh_body.read_text()
        self.assertIn("**As of ", body)
        for r in self.REPOS:
            self.assertIn(f"| {r} | PASS |", body)

    def test_no_repo_is_silently_dropped(self):
        """Every repo in the run has a row; the table is what the board sees."""
        out = self.run_health("--dry-run", "--only", "portfolio", *self.cmds())
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("| portfolio | PASS |", out.stdout)
        self.assertNotIn("| nvim-trainer |", out.stdout)


if __name__ == "__main__":
    unittest.main()


class TestSca(Base):
    HIGH = {"id": "GHSA-high", "aliases": ["CVE-2026-1111"],
            "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
            "affected": [{"ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "27.4.0"}]}]}],
            "published": "2026-10-01T09:00:00Z"}
    KEV = {"id": "GO-kev", "aliases": ["CVE-2026-2222"], "severity": None,
           "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.2.0"}]}]}],
           "published": "2026-09-01T00:00:00Z"}
    MOD = {"id": "GHSA-mod", "aliases": ["CVE-2026-3333"],
           "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:L/A:N"}],
           "affected": [{"ranges": [{"events": [{"introduced": "0"}, {"fixed": "1.65.0"}]}]}],
           "published": "2026-08-01T00:00:00Z"}

    # The host pins host_pins() would find, in the order the code appends them (docker, runc,
    # containerd, openssh); index 4 is docker, 5 runc, 2 playwright, in the queryset the stub sees.
    HOSTQ = [("docker", "Go", "github.com/docker/docker", "27.3.1", "docker version"),
             ("runc", "Go", "github.com/opencontainers/runc", "1.1.12", "runc --version"),
             ("containerd", "Go", "github.com/containerd/containerd", "1.7.24", "containerd --version"),
             ("openssh-server", "Ubuntu", "openssh", "1:9.6p1-3ubuntu13", "dpkg -s openssh-server")]

    def repo(self):
        root = self.t / "repo"
        (root / "sandbox").mkdir(parents=True, exist_ok=True)
        (root / "vps").mkdir(exist_ok=True)
        (root / "sandbox" / "Dockerfile").write_text(
            "FROM ubuntu:26.04@sha256:abc\nARG GITLEAKS_VERSION=8.30.1\nARG MISE_VERSION=2026.10.19\n"
            "ARG NODE_VERSION=24.22.2\nARG PLAYWRIGHT_VERSION=1.64.1\nARG PONYTAIL_TAG=v5.3.3\n")
        (root / "vps" / "compose.yaml").write_text(
            "services:\n  hermes:\n    image: nousresearch/hermes-agent:v0.22.5@sha256:9c55\n"
            "  hq:\n    image: caddy:2.11.7-alpine@sha256:e70b\n")
        return root

    def responses(self, *at):
        """A querybatch reply for the 8 queries, with an advisory at each index given."""
        results = [{} for _ in range(8)]
        for i, vid in at:
            results[i] = {"vulns": [{"id": vid, "modified": "2026-10-01T00:00:00Z"}]}
        return {"results": results}

    def run_sca(self):
        argv = ["--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--repo-root", str(self.repo()), "--github", self.url + "/gh",
                "--osv", self.url + "/osv/v1", "--kev", self.url + "/kev/kev.json"]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(sca, "host_pins", return_value=(list(self.HOSTQ), ["docker version: test"])), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sca.main(argv)
        for text in (out.getvalue(), err.getvalue()):
            self.assertNotIn(GH, text)
        return code, out.getvalue()

    def test_reads_the_repo_pins_and_notes_the_rest(self):
        queries, notes = sca.repo_pins(self.repo())
        self.assertEqual([q[0] for q in queries], ["gitleaks", "mise", "playwright", "caddy"])
        self.assertIn(("caddy", "Go", "github.com/caddyserver/caddy", "2.11.7", "vps/compose.yaml"), queries)
        self.assertIn(("playwright", "npm", "playwright", "1.64.1", "sandbox/Dockerfile"), queries)
        self.assertTrue(any("ubuntu:26.04" in n for n in notes))
        self.assertTrue(any("Node runtime 24.22.2" in n for n in notes))
        self.assertTrue(any("Ponytail" in n for n in notes))
        self.assertTrue(any("hermes-agent" in n for n in notes))

    def test_reads_the_host_versions(self):
        def fake(cmd):
            return {"docker": "Client:\n Version: 27.3.1\nServer:\n Engine:\n  Version: 28.0.1\n",
                    "runc": "runc version 1.1.12\ncommit: v1.1.12-0-g1d9c0e3\n",
                    "containerd": "containerd github.com/containerd/containerd v1.7.24 a88e0e0\n",
                    "dpkg": "Package: openssh-server\nSource: openssh\nVersion: 1:9.6p1-3ubuntu13\n"}[cmd[0]]
        with mock.patch.object(sca, "run", side_effect=fake):
            queries, _ = sca.host_pins()
        self.assertIn(("docker", "Go", "github.com/docker/docker", "28.0.1", "docker version"), queries)   # the server
        self.assertIn(("runc", "Go", "github.com/opencontainers/runc", "1.1.12", "runc --version"), queries)
        self.assertIn(("openssh-server", "Ubuntu", "openssh", "1:9.6p1-3ubuntu13", "dpkg -s openssh-server"), queries)

    def test_cvss_v3_base_scores(self):
        self.assertEqual(sca.cvss_base("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"), 9.8)
        self.assertEqual(sca.cvss_base("CVSS:3.1/AV:N/AC:H/PR:N/UI:N/S:U/C:H/I:H/A:H"), 8.1)
        self.assertLess(sca.cvss_base("CVSS:3.1/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:L/A:N"), 7.0)
        self.assertIsNone(sca.cvss_base("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N"))

    def test_keeps_high_and_kev_drops_the_rest_and_writes_one_pinned_issue(self):
        self.stub.osv_batch = self.responses((4, "GHSA-high"), (5, "GO-kev"), (2, "GHSA-mod"))
        self.stub.osv_vulns = {"GHSA-high": self.HIGH, "GO-kev": self.KEV, "GHSA-mod": self.MOD}
        self.stub.kev = {"catalogVersion": "test", "vulnerabilities": [{"cveID": "CVE-2026-2222"}]}
        code, out = self.run_sca()
        self.assertEqual(code, 0, out)
        (n, issue), = self.stub.issues.items()
        self.assertEqual((issue["title"], self.stub.labels(n), self.stub.pinned), ("Supply-chain", set(), f"node-{n}"))
        self.assertIn("| docker | 27.3.1 | [GHSA-high]", issue["body"])
        self.assertIn("9.8", issue["body"])
        self.assertIn("27.4.0", issue["body"])           # the fix
        self.assertIn("2026-10-01", issue["body"])       # and its date
        self.assertIn("[GO-kev]", issue["body"])
        self.assertIn("(KEV)", issue["body"])
        self.assertNotIn("GHSA-mod", issue["body"])      # CVSS below 7.0 and not KEV
        self.assertIn("1 advisory/advisories below CVSS 7.0", issue["body"])
        self.assertIn("Pins checked:", issue["body"])
        self.assertIn("ubuntu:26.04", issue["body"])     # the digest pin, noted not queried

    def test_a_clear_run_updates_the_same_issue_in_place(self):
        self.stub.osv_batch = self.responses()
        self.run_sca()
        (n, issue), = self.stub.issues.items()
        self.assertIn("**Clear, ", issue["body"])
        self.stub.osv_batch = self.responses((4, "GHSA-high"))
        self.stub.osv_vulns = {"GHSA-high": self.HIGH}
        code, out = self.run_sca()
        self.assertEqual(code, 0, out)
        (n2, issue), = self.stub.issues.items()          # still one issue, not two
        self.assertEqual(n2, n)
        self.assertNotIn("Clear,", issue["body"])
        self.assertIn("| docker | 27.3.1 | [GHSA-high]", issue["body"])

    def test_a_run_with_no_findings_still_writes_a_dated_clear_line(self):
        self.stub.osv_batch = self.responses()
        code, out = self.run_sca()
        self.assertEqual(code, 0, out)
        (_, issue), = self.stub.issues.items()
        self.assertIn("**Clear, ", issue["body"])
        self.assertEqual(self.stub.todo_count(), 0)
