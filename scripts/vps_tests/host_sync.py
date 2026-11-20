"""vps/sync.sh: what the VPS applies from main, what it refuses, and the Sync issue it keeps."""

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from .base import ROOT
from .stub import GH, ORKEY, HKEY, Stub, serve


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
        self.checks = [{"name": n, "status": "completed", "conclusion": "success", "app": {"slug": "github-actions"}} for n in (
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
        self.commit({"vps/install.sh": "x", "vps/compose.yaml": "y", "vps/hq-status.py": "z"})
        p = self.accept()
        self.assertEqual(p.returncode, 0, p.stderr)
        pins = (self.t / "pins.toml").read_text()
        self.assertIn("vps/install.sh", pins)
        self.assertIn("vps/compose.yaml", pins)
        self.assertNotIn("vps/hq-status.py", pins)      # not protected: never needs a pin
        self.assertEqual(oct((self.t / "pins.toml").stat().st_mode & 0o777), "0o600")
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)             # up, then the Impeccable prune
        self.assertIn("Applied", self.sync_issue()["body"])

    def test_a_normal_change_applies_by_itself_without_any_pin(self):
        """Scripts, the sandbox image and Hermes config need no --accept (compose and units do)."""
        self.commit({"sandbox/Dockerfile": "d", "hermes/config.yaml": "h", "vps/hq-status.py": "p"})
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(len(calls), 2)             # compose up, the Impeccable prune
        self.assertFalse((self.t / "pins.toml").exists())
        self.assertEqual(self.head(), self.origin_head())
        self.assertIn("Applied", self.sync_issue()["body"])

    def test_the_protected_set_is_the_sync_its_installers_units_compose_root_scripts_workflows_and_sudoers(self):
        # (vps/sync.sh itself is covered by the replay test: a bogus copy could not run.)
        for path in ("vps/install.sh", "vps/install-units.sh", "vps/secret-modes.sh", "vps/systemd/section9-sync.timer",
                     "vps/systemd/section9-sync.service", "vps/systemd/section9-dispatch.service", "vps/apt/52section9-x",
                     "vps/compose.yaml", "vps/health.sh", "vps/dashboard-prep.sh", "vps/cloudflared-patch.sh",
                     "vps/container-runtime-patch.sh", ".github/workflows/ci.yml", "vps/sudoers.d/section9"):
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
        self.commit({"sandbox/Dockerfile": "x"}, when=f"{now - 10 * 60} +0000")
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

    COMPOSE = ("services:\n  hermes:\n    image: nousresearch/hermes-agent:v1.0@sha256:{a}\n"
               "  hq:\n    image: caddy:2.1-alpine@sha256:{b}\n    read_only: true\n")

    def pinned_compose(self):
        self.commit({"vps/compose.yaml": self.COMPOSE.format(a="a" * 64, b="b" * 64)})
        self.accept()
        self.assertEqual(self.sync()[0].returncode, 0)

    def test_a_compose_change_that_only_moves_image_versions_applies_without_a_pin(self):
        self.pinned_compose()
        self.commit({"vps/compose.yaml": self.COMPOSE.format(a="c" * 64, b="d" * 64).replace("v1.0", "v1.1")})
        p, calls = self.sync()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.head(), self.origin_head())
        self.assertTrue(calls[-2].endswith("up -d --build --remove-orphans"), calls)
        self.commit({"vps/compose.yaml": self.COMPOSE.format(a="e" * 64, b="d" * 64)})   # and again, from there
        self.assertEqual(self.sync()[0].returncode, 0)

    def test_any_other_compose_change_needs_a_pin(self):
        self.pinned_compose()
        for text in (self.COMPOSE.replace("read_only: true", "privileged: true"),
                     self.COMPOSE.replace("caddy:", "evil/caddy:"),
                     self.COMPOSE + "    volumes: [/var/run/docker.sock:/var/run/docker.sock]\n"):
            before = self.head()
            self.commit({"vps/compose.yaml": text.format(a="a" * 64, b="b" * 64)})
            p, calls = self.sync()
            self.assertEqual((p.returncode, self.head()), (1, before), text)
            self.assertIn("vps/compose.yaml changed", p.stderr)
            self.git("reset", "-q", "--hard", "HEAD~1", cwd=self.t / "origin")

    def test_a_required_check_counts_only_from_github_actions(self):
        before = self.head()
        self.commit({"docs/RUNBOOK.md": "x"})
        self.checks[-1]["app"] = {"slug": "some-other-app"}     # a green "VPS stack", but not from CI
        p, _ = self.sync()
        self.assertEqual((p.returncode, self.head()), (0, before))
        self.assertIn("missing: VPS stack", p.stdout)
        del self.checks[-1]["app"]
        self.assertIn("missing: VPS stack", self.sync()[0].stdout)

    def test_the_sync_issue_says_whether_the_secrets_are_root_only(self):
        self.sync()
        self.assertIn("modes: WRONG", self.sync_issue()["body"])     # the test's secrets are not root-only
        self.env["S9_SECRETS_OWNER"] = str(os.getuid())
        for f in (self.t / "secrets").iterdir():
            f.chmod(0o600)
        (self.t / "secrets").chmod(0o700)
        self.commit({"docs/RUNBOOK.md": "x"})
        self.sync()
        self.assertIn("modes: ok (root-only)", self.sync_issue()["body"])
