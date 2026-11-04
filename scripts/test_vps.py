"""Tests for vps/dispatch.py and sandbox/s9-task against stub GitHub, Hermes and OpenRouter.

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
        self.page_size, self.link = None, None     # page_size: serve issue lists in pages of this size
        self.hermes = "report"      # report | silent | slow | error
        self.next = 1
        self.gh_comments, self.pinned, self.crew_auth = [], None, "unset"
        self.crew_sha, self.checks = "tree1", []
        self.hq_sha, self.hq = "hq1", {"ghost.yaml": "name: ghost\nlabel: Batou's\n", "notes.md": "x", "sub/deep.yaml": "name: no\n"}
        self.crew = {"index.html": "<h1>crew</h1>", "status.json": "SAMPLE", "css/app.css": "a{}"}

    def add(self, labels, title="Do a thing", state="open", age_min=None):
        n = self.next
        self.next += 1
        updated = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min or 0)   # without age_min: a fixed stamp, or "now" for status:working
        self.issues[n] = {"number": n, "title": title, "body": f"body of {n}", "state": state, "updated_at": "2026-11-01T10:00:00Z" if age_min is None and "status:working" not in labels else updated.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
        if path == "/or/key" and self.key is not None:
            return 200, self.key
        if path == "/or/key":
            return 200, {"data": {"usage": self.monthly, "limit": 20, "limit_remaining": self.remaining,
                                  "usage_monthly": self.monthly, "usage_daily": self.daily}}
        if path == "/hermes/v1/models":
            return 200, {"data": []}
        if path == "/hermes/v1/chat/completions":
            self.hermes_calls.append((body, auth, headers.get("X-Hermes-Session-Id")))
            self.monthly += self.bump          # a run's cost shows up in the counter it returns
            n = int(body["messages"][1]["content"].split("#")[1].split(":")[0])
            if self.hermes == "error":
                return 500, {"error": f"upstream said {ORKEY}"}
            if self.hermes == "slow":
                time.sleep(2.5)
            if self.hermes == "report":     # what the agent does with s9-task
                self.issues[n]["labels"] = [{"name": x} for x in self.labels(n) - {"status:working"}] + [{"name": "status:review"}]
            return 200, {"choices": [{"message": {"content": "line 1\nline 2\nall done"}}],
                         "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        return 404, {}


def serve(stub):
    class H(BaseHTTPRequestHandler):
        def any(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            u = urlsplit(self.path)
            stub.link = None
            code, out = stub.handle(self.command, u.path, parse_qs(u.query), body, self.headers.get("Authorization"), self.headers)
            raw = json.dumps(out).encode()
            self.send_response(code)
            if stub.link:
                self.send_header("Link", stub.link)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
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

    def run_dispatch(self, cmd="run", limits=None):
        argv = [cmd, "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--roster", str(self.t / "roster.toml"), "--private-routines", str(self.t / "none.toml"),
                "--limits", limits or str(self.t / "none-limits.toml"),
                "--github", self.url + "/gh", "--openrouter", self.url + "/or", "--hermes", self.url + "/hermes"]
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
        with open(self.t / "state" / "dispatch.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("in progress", out)

    def test_status_reports_queue_and_hermes(self):
        self.stub.add(["agent:batou", "status:todo"])
        _, out = self.run_dispatch("status")
        self.assertIn("1 todo", out)
        self.assertIn("hermes: reachable", out)

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

    def test_timeout_blocks(self):
        self.stub.hermes = "slow"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("timed out after 1 s", self.stub.comments[0][1])


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
                    "S9_DISPATCH_PY": str(ROOT / "vps" / "dispatch.py"),
                    "PATH": f"{t / 'bin'}:{os.environ['PATH']}"}
        (t / "secrets").mkdir()
        (t / "secrets" / "sandbox.env").write_text(f"GH_TOKEN={GH}\n")
        (t / "secrets" / "hermes.env").write_text(f"OPENROUTER_API_KEY={ORKEY}\nAPI_SERVER_KEY={HKEY}\n")
        (t / "bin").mkdir()
        (t / "bin" / "docker").write_text(f'#!/bin/sh\necho "docker $@" >> {t}/calls\n[ ! -e {t}/docker-fails ]\n')
        (t / "bin" / "docker").chmod(0o755)
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

    def commit(self, files):
        for name, text in files.items():
            f = self.t / "origin" / name
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(text)
            if name.endswith(".sh"):
                f.chmod(0o755)
        self.git("add", "-A", cwd=self.t / "origin")
        self.git("commit", "-q", "-m", "change", cwd=self.t / "origin")

    def accept(self):
        """What Petter runs on the VPS after reading a change to a guarded path."""
        return subprocess.run(["bash", str(ROOT / "vps" / "sync.sh"), "--accept"], env=self.env, capture_output=True, text=True)

    def sync(self):
        p = subprocess.run(["bash", str(ROOT / "vps" / "sync.sh")], env=self.env, capture_output=True, text=True)
        calls = (self.t / "calls").read_text().splitlines() if (self.t / "calls").exists() else []
        return p, calls

    def sync_issue(self):
        return next(i for i in self.stub.issues.values() if i["title"] == "Sync")

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

    def test_guarded_change_is_refused_until_a_human_pins_it(self):
        before = self.head()
        self.commit({"vps/compose.yaml": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (1, []))
        self.assertIn("does not pin", p.stderr)
        self.assertEqual(self.head(), before)       # the checkout is untouched
        issue = self.sync_issue()
        self.assertEqual({l["name"] for l in issue["labels"]}, {"status:blocked"})
        self.assertIn("vps/compose.yaml is new", issue["body"])
        self.assertIn("--accept", issue["body"])

    def test_accept_pins_what_a_human_read_and_the_sync_then_applies(self):
        self.commit({"vps/compose.yaml": "x"})
        p = self.accept()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("vps/compose.yaml", (self.t / "pins.toml").read_text())
        self.assertEqual(oct((self.t / "pins.toml").stat().st_mode & 0o777), "0o600")
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)             # up, then the Impeccable prune
        self.assertIn("Applied", self.sync_issue()["body"])

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
        self.assertIn("timed out after 1 s", self.stub.comments[0][1])

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
        for leak in ("private errand", "secret-garden", "Morning ops", "$", "sk-or", "section9-hq"):
            self.assertNotIn(leak, raw)

    def test_tasks_carry_status_agent_and_a_public_pr_and_nothing_private(self):
        self.board()
        (self.t / "secrets" / "crew-deny.txt").write_text("secret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        tasks = self.status()["tasks"]
        by_title = {t["title"]: t for t in tasks}
        for t in tasks:
            self.assertEqual(set(t), {"id", "title", "agent", "status", "pr", "updated"})
        self.assertEqual((by_title["Fix the crew page layout"]["status"], by_title["Fix the crew page layout"]["agent"]),
                         ("working", "batou"))
        self.assertEqual(by_title["Add dark mode"]["status"], "review")
        self.assertEqual(by_title["Add dark mode"]["pr"], "https://github.com/petterfranzen/ghost-in-the-machine/pull/12")
        self.assertEqual(by_title["Nightly build"]["status"], "done")
        private = [t for t in tasks if t["title"] == "On a private task"]
        self.assertEqual(len(private), 3)       # the label, the deny term, the private report
        self.assertTrue(all(t["pr"] is None for t in private))
        raw = json.dumps(tasks)
        for leak in ("private errand", "secret-garden", "Morning ops", "$", "section9-hq"):
            self.assertNotIn(leak, raw)

    def test_without_a_deny_file_task_titles_are_not_published(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        self.assertEqual({t["title"] for t in self.status()["tasks"]}, {"On a task", "On a private task"})

    def test_status_also_writes_the_board_page_and_a_copy_of_status_json(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        site = self.t / "site"
        self.assertEqual((site / "board" / "status.json").read_text(), (site / "status.json").read_text())
        self.assertEqual((site / "board" / "index.html").read_bytes(), (ROOT / "vps" / "board" / "index.html").read_bytes())

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
        self.assertEqual((row["v"], row["issue"], row["agent"], row["role"], row["type"], row["model"]),
                         (1, n, "Batou", "", "qa", "hermes-agent"))
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
    def capacity(self):
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        return out

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


if __name__ == "__main__":
    unittest.main()
