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
            return 200, {"total_count": len(self.checks), "check_runs": self.checks}
        if path.startswith("/gh/"):
            return self.gh(method, path, query, body, auth)
        if path == "/or/key" and self.key is not None:
            return 200, self.key
        if path == "/or/key":
            return 200, {"data": {"usage": 5, "limit": 20, "limit_remaining": self.remaining,
                                  "usage_monthly": 5, "usage_daily": 0.25}}
        if path == "/hermes/v1/models":
            return 200, {"data": []}
        if path == "/hermes/v1/chat/completions":
            self.hermes_calls.append((body, auth, headers.get("X-Hermes-Session-Id")))
            n = int(body["messages"][1]["content"].split("#")[1].split(":")[0])
            if self.hermes == "error":
                return 500, {"error": f"upstream said {ORKEY}"}
            if self.hermes == "slow":
                time.sleep(2.5)
            if self.hermes == "report":     # what the agent does with s9-task
                self.issues[n]["labels"] = [{"name": x} for x in self.labels(n) - {"status:working"}] + [{"name": "status:review"}]
            return 200, {"choices": [{"message": {"content": "line 1\nline 2\nall done"}}]}
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

    def run_dispatch(self, cmd="run"):
        argv = [cmd, "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--roster", str(self.t / "roster.toml"), "--private-routines", str(self.t / "none.toml"),
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
                    "PATH": f"{t / 'bin'}:{os.environ['PATH']}"}
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

    def sync(self):
        p = subprocess.run(["bash", str(ROOT / "vps" / "sync.sh")], env=self.env, capture_output=True, text=True)
        calls = (self.t / "calls").read_text().splitlines() if (self.t / "calls").exists() else []
        return p, calls

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
        self.assertEqual(self.sync()[1], ["install-units"])
        (self.t / "calls").unlink()
        self.commit({"docs/RUNBOOK.md": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (0, []))
        self.assertIn("applied", p.stdout)

    def test_hermes_or_sandbox_change_rebuilds(self):
        self.commit({"sandbox/Dockerfile": "x"})
        self.assertEqual(len(self.sync()[1]), 2)        # up, then the Impeccable prune
        self.commit({"hermes/config.yaml": "x"})
        self.assertEqual(len(self.sync()[1]), 4)

    def test_failed_apply_is_retried_three_times_then_stops_until_main_moves(self):
        (self.t / "docker-fails").write_text("")
        self.commit({"vps/compose.yaml": "x"})
        for _ in range(3):
            self.assertEqual(self.sync()[0].returncode, 1)
        self.assertEqual(len(self.sync()[1]), 3)
        p, calls = self.sync()
        self.assertEqual((p.returncode, len(calls)), (1, 3))
        self.assertIn("not retrying", p.stderr)
        (self.t / "docker-fails").unlink()
        self.commit({"vps/compose.yaml": "y"})      # main moves: tries again, from the last applied commit
        p, calls = self.sync()
        self.assertEqual((p.returncode, len(calls)), (0, 5))

    def test_dirty_checkout_is_refused(self):
        (self.t / "repo" / "README.md").write_text("hand edit")
        self.commit({"vps/compose.yaml": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (1, []))
        self.assertIn("local changes", p.stderr)


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
        self.assertEqual(set(doc), {"generatedAt", "budget", "model", "agents"})
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


if __name__ == "__main__":
    unittest.main()
