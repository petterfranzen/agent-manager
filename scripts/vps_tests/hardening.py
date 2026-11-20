"""Root-side hardening: tokens kept out of the health run's builds (scripts/health-run.sh,
vps/health.sh) and the secrets directory's modes (vps/secret-modes.sh)."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from .base import ROOT

TOKEN = "github_pat_" + "t" * 22
CS = "cs-" + "c" * 20


def stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


class TestHealthTokens(unittest.TestCase):
    """The builds never see a token; only `gh api` gets GH_TOKEN."""

    SCRIPT = ROOT / "scripts" / "health-run.sh"
    REPOS = ("ghost-in-the-machine", "nvim-trainer", "portfolio", "dinner-planner", "flight-tracker")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = Path(tmp.name)
        for r in self.REPOS:
            (self.t / "repos" / r).mkdir(parents=True)
        (self.t / "bin").mkdir()
        # gh records whether it had the token, and keeps the body it was asked to post.
        stub(self.t / "bin" / "gh",
             f'echo "gh token=${{GH_TOKEN:+set}} $*" >> {self.t}/gh.log\n'
             f'for a in "$@"; do case "$a" in body=@*) cp "${{a#body=@}}" {self.t}/posted;; esac; done\n'
             'case "$*" in *"-f title="*) echo \'{"number":7,"node_id":"N1"}\';; esac\n')
        self.env = dict(os.environ, GH_TOKEN=TOKEN, GITHUB_TOKEN=TOKEN, CS_ACCESS_TOKEN=CS,
                        PATH=f"{self.t / 'bin'}:{os.environ['PATH']}")

    def health(self, *args, stdin=None):
        cmds = []
        for r in self.REPOS:
            cmds += ["--cmd", f"{r}=env > {self.t}/env-{r}"]
        return subprocess.run([str(self.SCRIPT), "--repos-dir", str(self.t / "repos"), *cmds, *args],
                              input=stdin, capture_output=True, text=True, env=self.env, timeout=120)

    def test_a_build_step_sees_no_token_but_gh_api_does(self):
        out = self.health()
        self.assertEqual(out.returncode, 0, out.stderr)
        for r in self.REPOS:
            env = (self.t / f"env-{r}").read_text()
            for name, value in (("GH_TOKEN", TOKEN), ("GITHUB_TOKEN", TOKEN), ("CS_ACCESS_TOKEN", CS)):
                self.assertNotIn(f"{name}=", env, r)
                self.assertNotIn(value, env, r)
        calls = (self.t / "gh.log").read_text().splitlines()
        self.assertTrue(calls and all(c.startswith("gh token=set ") for c in calls), calls)

    def test_body_only_prints_the_body_and_posts_nothing(self):
        out = self.health("--body-only")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.startswith("**As of "), out.stdout[:80])
        self.assertIn("| portfolio | PASS |", out.stdout)
        self.assertFalse((self.t / "gh.log").exists())

    def test_post_sends_a_body_from_stdin_and_builds_nothing(self):
        body = "**As of x**\n\n| Repo | Result | Time | Ran |\n|---|---|---|---|\n| portfolio | FAIL | 1s | b |\n"
        out = self.health("--post", "-", stdin=body)
        self.assertEqual(out.returncode, 1)                     # a FAIL row is a non-zero exit
        self.assertIn("1 repo(s) FAIL", out.stderr)
        self.assertEqual((self.t / "posted").read_text(), body)
        self.assertEqual(list(self.t.glob("env-*")), [])        # no step ran

    def test_post_refuses_an_empty_body(self):
        out = self.health("--post", "-", stdin="")
        self.assertEqual(out.returncode, 1)
        self.assertIn("empty body", out.stderr)
        self.assertFalse((self.t / "gh.log").exists())

    def test_the_vps_wrapper_blanks_the_tokens_for_the_build_container_only(self):
        stub(self.t / "bin" / "docker",
             f'echo "$*" >> {self.t}/docker.log\n'
             'case "$*" in *--body-only*) echo body;; *) cat > /dev/null;; esac\n')
        out = subprocess.run([str(ROOT / "vps" / "health.sh")], capture_output=True, text=True,
                             env=dict(self.env, S9_REPO_DIR=str(ROOT)), timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        build, post = (self.t / "docker.log").read_text().splitlines()
        self.assertIn("-e GH_TOKEN= -e CS_ACCESS_TOKEN= sandbox --body-only", build)
        self.assertTrue(post.endswith(" sandbox --post -"), post)
        self.assertNotIn("TOKEN", post)
        for call in (build, post):
            self.assertIn("run --rm --no-deps -T --user agent", call)
            self.assertIn("--entrypoint /usr/local/bin/health-run.sh", call)
        unit = (ROOT / "vps" / "systemd" / "section9-health.service").read_text()
        self.assertIn("ExecStart=/opt/agent-manager/vps/health.sh\n", unit)


class TestSecretModes(unittest.TestCase):
    """vps/secret-modes.sh: everything in the secrets directory is root-only."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.d = Path(tmp.name) / "section9"
        (self.d / "briefs").mkdir(parents=True)
        files = {"hermes.env": 0o600, "sandbox.env": 0o600, "codescene.env": 0o600, "limits.toml": 0o400,
                 "pins.toml": 0o600, "sandbox_authorized_keys": 0o644, "briefs/Togusa.md": 0o600}
        for name, mode in files.items():
            (self.d / name).write_text("x")
            (self.d / name).chmod(mode)
        for d in (self.d, self.d / "briefs"):
            d.chmod(0o700)
        self.env = dict(os.environ, S9_SECRETS_OWNER=str(os.getuid()))

    def check(self):
        return subprocess.run([str(ROOT / "vps" / "secret-modes.sh"), str(self.d)],
                              capture_output=True, text=True, env=self.env)

    def test_root_only_files_and_directories_pass(self):
        out = self.check()
        self.assertEqual((out.returncode, out.stdout), (0, ""))

    def test_each_loose_file_or_directory_is_named(self):
        (self.d / "limits.toml").chmod(0o644)
        (self.d / "briefs" / "Togusa.md").chmod(0o640)
        self.d.chmod(0o755)
        out = self.check()
        self.assertEqual(out.returncode, 1)
        self.assertEqual(len(out.stdout.splitlines()), 3, out.stdout)
        for path in ("limits.toml", "briefs/Togusa.md"):
            self.assertIn(f"{self.d / path}: uid:mode is {os.getuid()}:6", out.stdout)
        self.assertIn(f"{self.d}: uid:mode is {os.getuid()}:755, want {os.getuid()}:700", out.stdout)

    def test_the_hermes_key_belongs_to_the_hermes_user_and_links_are_refused(self):
        (self.d / "hermes_ssh_key").write_text("k")
        (self.d / "hermes_ssh_key").chmod(0o400)
        (self.d / "link.env").symlink_to(self.d / "hermes.env")
        out = self.check()
        self.assertEqual(out.returncode, 1)
        self.assertIn("hermes_ssh_key: uid:mode is", out.stdout)
        self.assertIn("want 10000:400", out.stdout)
        self.assertIn("link.env: not a plain file or directory", out.stdout)

    def test_a_missing_directory_fails(self):
        out = subprocess.run([str(ROOT / "vps" / "secret-modes.sh"), str(self.d / "nope")],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 1)
        self.assertIn("missing", out.stdout)

    def test_install_checks_the_whole_directory_and_the_sync_issue_reports_it(self):
        install = (ROOT / "vps" / "install.sh").read_text()
        self.assertIn('"$here/secret-modes.sh" "$secrets"', install)
        self.assertLess(install.index("chmod 0644"), install.index("secret-modes.sh"))
        self.assertLess(install.index("secret-modes.sh"), install.index("docker compose"))
        self.assertIn('"$check" "$SECRETS_DIR"', (ROOT / "vps" / "sync.sh").read_text())
