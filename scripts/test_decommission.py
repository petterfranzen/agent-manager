"""The decommission step of deploy/vm-sync.sh, run against fake docker/systemctl.

Nothing here touches a real system: every path the script uses is overridden
through AM_* variables, and docker/systemctl are stubs that log their calls.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy" / "vm-sync.sh"

FAKE = """#!/bin/sh
echo "$(basename "$0") $*" >> "$FAKE_LOG"
[ "$(basename "$0")" = docker ] || exit 0
case "$*" in
  "volume ls"*) echo agent-manager_paperclip-data ;;
  "volume inspect"*) echo "$FAKE_MOUNT" ;;
  "compose down"*) [ -z "${FAKE_DOWN_FAILS:-}" ] || exit 1 ;;
esac
exit 0
"""


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


class Decommission(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        t = self.t = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        for d in ("bin", "state", "secrets", "sbin", "units", "backups", "mount"):
            (t / d).mkdir()
        for name in ("docker", "systemctl"):
            (t / "bin" / name).write_text(FAKE)
            (t / "bin" / name).chmod(0o755)
        self.log = t / "calls.log"
        self.log.write_text("")

        # The agent stack's files, and flight-tracker's, which must survive.
        for f in ("postgres.env", "paperclip.env", "hermes.env", "sandbox.env",
                  "paperclip-board.env", "hermes_ssh_key", "sandbox_authorized_keys"):
            (t / "secrets" / f).write_text("x")
        (t / "secrets" / "bridge-keys").mkdir()
        (t / "secrets" / "bridge-keys" / "batou.key").write_text("x")
        (t / "secrets" / "other-app.env").write_text("keep")
        for f in ("agent-manager-spend.service", "agent-manager-spend.timer",
                  "agent-manager-crew-status.service", "agent-manager-sync.timer",
                  "flight-tracker.service"):
            (t / "units" / f).write_text("x")
        for f in ("agent-manager-spend", "agent-manager-crew-status",
                  "agent-manager-heal-accounting", "agent-manager-sync"):
            (t / "sbin" / f).write_text("x")
        (t / "flight-data").mkdir()
        (t / "flight-data" / "cache.db").write_text("data")
        b = t / "mount" / "instances" / "default" / "data" / "backups"
        b.mkdir(parents=True)
        (b / "paperclip-20261030-010000.sql.gz").write_text("old")
        (b / "paperclip-20261031-020000.sql.gz").write_text("newest")
        s = t / "mount" / "instances" / "default" / "secrets"
        s.mkdir(parents=True)
        (s / "master.key").write_text("key")

        # A checkout whose origin/main equals HEAD and is recorded as applied.
        origin = t / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
        self.repo = t / "repo"
        subprocess.run(["git", "clone", "-q", str(origin), str(self.repo)], check=True,
                       capture_output=True)
        (self.repo / "deploy").mkdir()
        (self.repo / "deploy" / "README").write_text("x")
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "init", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)

    def mark_decommissioned(self):
        (self.repo / "deploy" / "DECOMMISSIONED").write_text("")
        git("add", ".", cwd=self.repo)
        git("commit", "-qm", "decommission", cwd=self.repo)
        git("push", "-q", "origin", "HEAD:main", cwd=self.repo)

    def run_sync(self, **extra):
        t = self.t
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
                             capture_output=True, text=True).stdout.strip()
        (t / "state").mkdir(exist_ok=True)
        state = t / "state" / "sync-state.json"
        if not state.exists():
            state.write_text('{"appliedSha": "%s", "lastAttemptSha": "%s", "attempts": 1, "result": "success"}' % (sha, sha))
        env = {
            "PATH": f"{t / 'bin'}:{os.environ['PATH']}",
            "AM_REPO_DIR": str(self.repo), "AM_STATE_DIR": str(t / "state"),
            "AM_LOCK_FILE": str(t / "lock"), "AM_SECRETS_DIR": str(t / "secrets"),
            "AM_SBIN_DIR": str(t / "sbin"), "AM_UNIT_DIR": str(t / "units"),
            "AM_BACKUP_DIR": str(t / "backups"), "AM_HEAL": str(t / "no-heal"),
            "FAKE_LOG": str(self.log), "FAKE_MOUNT": str(t / "mount"),
            **extra,
        }
        return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True)

    def calls(self):
        return self.log.read_text().splitlines()

    def left(self, *names):
        return [n for n in names if (self.t / "secrets" / n).exists()]

    def test_gated_off_does_nothing(self):
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), [])
        self.assertEqual(len(self.left("postgres.env", "paperclip-board.env")), 2)
        self.assertTrue((self.t / "units" / "agent-manager-spend.timer").exists())
        self.assertEqual(list((self.t / "backups").iterdir()), [])

    def test_runs_in_order_and_removes_the_stack(self):
        self.mark_decommissioned()
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        calls = self.calls()
        down = next(i for i, c in enumerate(calls) if c.startswith("docker compose down"))
        stop = next(i for i, c in enumerate(calls) if c == "systemctl stop agent-manager-spend.timer")
        self.assertLess(down, stop)
        self.assertIn("docker compose down --volumes --rmi all --remove-orphans", calls)
        for u in ("agent-manager-spend.timer", "agent-manager-spend.service",
                  "agent-manager-crew-status.service"):
            self.assertIn(f"systemctl stop {u}", calls)
            self.assertIn(f"systemctl disable {u}", calls)
            self.assertFalse((self.t / "units" / u).exists(), u)
        for f in ("agent-manager-spend", "agent-manager-crew-status", "agent-manager-heal-accounting"):
            self.assertFalse((self.t / "sbin" / f).exists(), f)
        self.assertEqual(self.left("postgres.env", "paperclip.env", "hermes.env", "sandbox.env",
                                   "paperclip-board.env", "hermes_ssh_key", "sandbox_authorized_keys",
                                   "bridge-keys"), [])
        # Backup: only the newest file, root-only; the sync stays.
        files = sorted(p.name for p in (self.t / "backups").iterdir())
        self.assertEqual(files, ["master.key", "paperclip-20261031-020000.sql.gz"])
        for p in (self.t / "backups").iterdir():
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.t / "backups" / "paperclip-20261031-020000.sql.gz").read_text(), "newest")
        self.assertTrue((self.t / "units" / "agent-manager-sync.timer").exists())
        self.assertTrue((self.t / "sbin" / "agent-manager-sync").exists())
        self.assertNotIn("systemctl stop agent-manager-sync.timer", calls)
        self.assertNotIn("systemctl disable agent-manager-sync.timer", calls)
        # An .env the stack does not own is kept, and said so.
        self.assertEqual(self.left("other-app.env"), ["other-app.env"])
        self.assertIn("left in place", r.stdout)

    def test_backup_comes_before_volumes_are_removed(self):
        self.mark_decommissioned()
        self.run_sync()
        calls = self.calls()
        self.assertLess(calls.index("docker volume ls -q --filter label=com.docker.compose.volume=paperclip-data"),
                        next(i for i, c in enumerate(calls) if c.startswith("docker compose down")))

    def test_idempotent(self):
        self.mark_decommissioned()
        self.assertEqual(self.run_sync().returncode, 0)
        first = self.calls()
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls(), first, "a second run must not run any command again")
        self.assertNotIn("decommission finished", r.stdout)

    def test_failed_step_stops_the_rest_and_is_retried(self):
        self.mark_decommissioned()
        r = self.run_sync(FAKE_DOWN_FAILS="1")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(len(self.left("postgres.env", "paperclip-board.env")), 2, "secrets kept")
        self.assertTrue((self.t / "units" / "agent-manager-spend.timer").exists())
        self.assertFalse(any(c.startswith("systemctl stop") for c in self.calls()))
        r = self.run_sync()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.left("postgres.env"), [])
        backups = [c for c in self.calls() if c.startswith("docker volume ls")]
        self.assertEqual(len(backups), 1, "the finished backup step is not repeated")

    def test_old_backups_are_pruned_after_30_days(self):
        self.mark_decommissioned()
        old = self.t / "backups" / "paperclip-20260101-000000.sql.gz"
        old.write_text("old")
        os.utime(old, (1, 1))
        self.run_sync()
        self.assertFalse(old.exists())
        self.assertTrue((self.t / "backups" / "paperclip-20261031-020000.sql.gz").exists())

    def test_flight_tracker_is_never_touched(self):
        self.mark_decommissioned()
        self.run_sync()
        self.run_sync()
        self.assertTrue((self.t / "units" / "flight-tracker.service").exists())
        self.assertEqual((self.t / "flight-data" / "cache.db").read_text(), "data")
        for c in self.calls():
            self.assertNotIn("flight", c.lower(), c)
        # The destructive code itself never names it.
        src = SCRIPT.read_text()
        body = src[src.index("# Decommission"):src.index("# Only one sync at a time")]
        code = "\n".join(l for l in body.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code.lower(), r"flight|cloudflared")

    def test_installer_skips_the_agent_units_once_decommissioned(self):
        src = (ROOT / "deploy" / "install-sync.sh").read_text()
        guard = src.index('if [ ! -e "$HERE/DECOMMISSIONED" ]; then')
        self.assertLess(guard, src.index("agent-manager-spend.service"))
        self.assertLess(guard, src.index("agent-manager-crew-status.service"))
        self.assertIn('[ -e "$HERE/DECOMMISSIONED" ] || systemctl enable --now agent-manager-spend.timer', src)
        self.assertIn("enable --now agent-manager-sync.timer", src)


if __name__ == "__main__":
    unittest.main()
