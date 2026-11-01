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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

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
        self.hermes = "report"      # report | silent | slow | error
        self.next = 1
        self.gh_comments, self.pinned, self.crew_auth = [], None, "unset"
        self.crew_sha, self.checks = "tree1", []
        self.crew = {"index.html": "<h1>crew</h1>", "status.json": "SAMPLE", "css/app.css": "a{}"}

    def add(self, labels, title="Do a thing", state="open"):
        n = self.next
        self.next += 1
        self.issues[n] = {"number": n, "title": title, "body": f"body of {n}", "state": state, "updated_at": "2026-11-01T10:00:00Z",
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
            return 200, [i for i in self.issues.values() if st in (i["state"], "all")
                         and (not want or want in self.labels(i["number"]))]
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
            code, out = stub.handle(self.command, u.path, parse_qs(u.query), body, self.headers.get("Authorization"), self.headers)
            raw = json.dumps(out).encode()
            self.send_response(code)
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
        self.commit({"vps/install-units.sh": f'#!/bin/sh\necho "install-units" >> {t}/calls\n', "README.md": "x"})
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
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0].startswith("docker compose -f ") and calls[0].endswith("vps/compose.yaml up -d --build --remove-orphans"))
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
        self.assertEqual(len(self.sync()[1]), 1)
        self.commit({"hermes/config.yaml": "x"})
        self.assertEqual(len(self.sync()[1]), 2)

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
        self.assertEqual((p.returncode, len(calls)), (0, 4))

    def test_dirty_checkout_is_refused(self):
        (self.t / "repo" / "README.md").write_text("hand edit")
        self.commit({"vps/compose.yaml": "x"})
        p, calls = self.sync()
        self.assertEqual((p.returncode, calls), (1, []))
        self.assertIn("local changes", p.stderr)


class TestHq(Base):
    def run_hq(self, cmd, site=None):
        argv = [cmd, "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                "--site-dir", str(site or self.t / "site"), "--repo-root", str(ROOT),
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


if __name__ == "__main__":
    unittest.main()
