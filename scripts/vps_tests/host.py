"""The VPS host scripts: s9-task, install.sh, cloudflared-patch.sh, dashboard-prep.sh and health-run.sh."""

import base64
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from .base import ROOT, Base
from .stub import GH


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


class TestCloudflaredPatch(unittest.TestCase):
    """vps/cloudflared-patch.sh: hold a cloudflared release for 24 h (patch rule)."""

    DELAY = 24 * 3600

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = Path(self.tmp.name)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.state = self.t / "state"
        self.apt_log = self.t / "apt.log"
        self._stub("dpkg-query", 'printf "%s\\n" "${STUB_INSTALLED:-}"')
        self._stub("apt-cache",
                   'printf "cloudflared:\\n  Installed: %s\\n  Candidate: %s\\n" '
                   '"${STUB_INSTALLED:-}" "${STUB_CANDIDATE:-}"')
        self._stub("apt-get", f'printf "%s\\n" "$*" >>"{self.apt_log}"')
        self.env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}",
                    "S9_STATE_DIR": str(self.state)}

    def _stub(self, name, body):
        p = self.bin / name
        p.write_text("#!/bin/sh\n" + body + "\n")
        p.chmod(0o755)

    def run_script(self, *args, installed="1.0", candidate="2.0"):
        env = {**self.env, "STUB_INSTALLED": installed, "STUB_CANDIDATE": candidate}
        return subprocess.run(["bash", str(ROOT / "vps" / "cloudflared-patch.sh"), *args],
                              env=env, capture_output=True, text=True)

    def stamp(self):
        return self.state / "cloudflared-candidate"

    def age_to(self, hours):
        self.stamp().write_text(f"2.0 {int(time.time()) - hours * 3600}\n")

    def test_a_new_release_is_recorded_and_held(self):
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("holding it for 24 h", p.stdout)
        self.assertEqual(self.stamp().read_text().split()[0], "2.0")
        self.assertFalse(self.apt_log.exists(), "nothing may be installed on first sight")

    def test_a_release_seen_for_24h_is_installed(self):
        self.run_script()
        self.age_to(25)
        p = self.run_script()
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("upgraded to 2.0", p.stdout)
        self.assertIn("--only-upgrade", self.apt_log.read_text())
        self.assertFalse(self.stamp().exists())

    def test_a_release_younger_than_24h_is_held(self):
        self.run_script()
        self.age_to(1)
        p = self.run_script()
        self.assertIn("holding 2.0", p.stdout)
        self.assertFalse(self.apt_log.exists())

    def test_now_lifts_the_delay(self):
        self.run_script()                       # a fresh candidate, seen only now
        p = self.run_script("--now")
        self.assertIn("upgraded to 2.0", p.stdout)
        self.assertIn("--only-upgrade", self.apt_log.read_text())

    def test_dry_run_changes_nothing(self):
        self.run_script()
        self.age_to(25)
        before = self.stamp().read_text()
        p = self.run_script("--dry-run")
        self.assertIn("would upgrade 1.0 -> 2.0", p.stdout)
        self.assertFalse(self.apt_log.exists())
        self.assertEqual(self.stamp().read_text(), before)

    def test_up_to_date_is_a_no_op(self):
        p = self.run_script(installed="2.0", candidate="2.0")
        self.assertIn("up to date", p.stdout)
        self.assertFalse(self.apt_log.exists())

    def test_status_only_reports(self):
        self.run_script()
        self.age_to(3)
        p = self.run_script("--status")
        self.assertIn("candidate 2.0", p.stdout)
        self.assertIn("seen 3 h ago", p.stdout)
        self.assertFalse(self.apt_log.exists())
        self.assertTrue(self.stamp().exists())

    def test_without_a_candidate_it_fails_closed(self):
        p = self.run_script(candidate="(none)")
        self.assertEqual(p.returncode, 1)
        self.assertIn("no candidate", p.stderr)

    def test_the_timer_and_the_apt_drop_in_are_installed(self):
        units = (ROOT / "vps" / "install-units.sh").read_text()
        self.assertIn("section9-cloudflared.timer", units)
        self.assertIn("apt/52section9-*", units)
        drop = (ROOT / "vps" / "apt" / "52section9-cloudflared").read_text()
        self.assertIn("Unattended-Upgrade::Package-Blacklist", drop)
        self.assertIn('"cloudflared";', drop)

    def test_the_container_runtime_is_held_by_its_own_script(self):
        units = (ROOT / "vps" / "install-units.sh").read_text()
        self.assertIn("section9-container-runtime-patch.timer", units)
        drop = (ROOT / "vps" / "apt" / "52section9-container-runtime").read_text()
        self.assertIn("Unattended-Upgrade::Package-Blacklist", drop)
        for pkg in ("docker-ce", "containerd.io", "docker.io", "containerd", "runc"):
            self.assertIn(f'"{pkg}";', drop)
        unit = (ROOT / "vps" / "systemd" / "section9-container-runtime-patch.service").read_text()
        self.assertIn("/opt/agent-manager/vps/container-runtime-patch.sh", unit)


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
        self.assertEqual(set(sample) - {"sample"}, {"generatedAt", "budget", "model", "agents", "tasks", "capacityAsOf"})
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
