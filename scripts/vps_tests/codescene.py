"""CodeScene's `cs` in the sandbox: the optional token file, the wrapper and the pins."""

import os
import re
import subprocess
import unittest
from unittest import mock

from .base import ROOT, Base, dispatcher

CS = "cs-" + "c" * 30
# Stands in for the real binary: shows what the wrapper handed it.
FAKE_CS = '#!/bin/sh\necho "token=${CS_ACCESS_TOKEN:-none} check=${CS_DISABLE_VERSION_CHECK:-unset} args=$*"\n'


class TestCodeSceneToken(Base):
    """The dispatcher's side: codescene.env is optional, and its token never reaches a log or the board."""

    def dispatch_one(self):
        self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatcher.net, "secrets", []):
            code, _ = self.run_dispatch()
            redacted = dispatcher.net.redact(f"cs said {CS}")
        self.assertEqual((code, len(self.stub.hermes_calls)), (0, 1))
        return redacted

    def test_the_token_is_redacted_when_the_file_is_there(self):
        (self.t / "secrets" / "codescene.env").write_text(f"CS_ACCESS_TOKEN={CS}\n")
        self.assertEqual(self.dispatch_one(), "cs said [redacted]")

    def test_a_missing_file_does_not_stop_the_run(self):
        self.assertIn(CS, self.dispatch_one())

    def test_a_file_without_the_token_does_not_stop_the_run(self):
        (self.t / "secrets" / "codescene.env").write_text("# not set up yet\nCS_ACCESS_TOKEN=\n")
        self.assertIn(CS, self.dispatch_one())


class TestCsWrapper(Base):
    """sandbox/cs: SSH sessions have no container environment, so the token comes from a 0600 file."""

    def cs(self, *args, token=None, env_token=None):
        fake, token_file = self.t / "fake-cs", self.t / "token"
        fake.write_text(FAKE_CS)
        fake.chmod(0o755)
        if token:
            token_file.write_text(token + "\n")
        env = {"PATH": os.environ["PATH"], "HOME": str(self.t), "CS_BIN": str(fake), "CS_TOKEN_FILE": str(token_file)}
        if env_token:
            env["CS_ACCESS_TOKEN"] = env_token
        p = subprocess.run(["sh", str(ROOT / "sandbox" / "cs"), *args], capture_output=True, text=True, env=env)
        self.assertEqual((p.returncode, p.stderr), (0, ""))
        return p.stdout.strip()

    def test_the_token_file_reaches_the_binary_with_the_arguments(self):
        self.assertEqual(self.cs("review", "a b.py", token=CS), f"token={CS} check=1 args=review a b.py")

    def test_no_token_file_still_runs(self):
        self.assertEqual(self.cs("version"), "token=none check=1 args=version")

    def test_a_token_already_in_the_environment_wins(self):
        self.assertEqual(self.cs("version", token=CS, env_token="from-env"), "token=from-env check=1 args=version")


class TestCsInstall(unittest.TestCase):
    """The files that carry it: the pinned download, the optional env file and the entrypoint."""

    def read(self, path):
        return (ROOT / path).read_text(encoding="utf-8")

    def test_the_download_is_pinned_by_commit_and_checksum(self):
        text = self.read("sandbox/Dockerfile")
        self.assertRegex(text, r"ARG CODESCENE_CLI_VERSION=\d+\.\d+\.\d+\n")
        self.assertRegex(text, r"ARG CODESCENE_CLI_COMMIT=[0-9a-f]{40}\n")
        self.assertRegex(text, r"ARG CODESCENE_CLI_SHA256=[0-9a-f]{64}\n")
        self.assertIn('cs-linux-amd64-${CODESCENE_CLI_COMMIT}.zip', text)
        self.assertIn('echo "${CODESCENE_CLI_SHA256}  cs.zip" | sha256sum -c -', text)
        self.assertNotIn("-latest.zip", text)
        self.assertIn("COPY --chmod=0755 cs /usr/local/bin/cs", text)
        self.assertEqual(text.count("CS_DISABLE_VERSION_CHECK=1"), 3)    # the build's check, ENV, pam_env
        self.assertNotIn("CS_ACCESS_TOKEN", text)                        # the token is never in the image

    def test_compose_takes_the_token_file_only_if_it_exists(self):
        text = self.read("vps/compose.yaml")
        sandbox = text.split("\n  sandbox:\n", 1)[1].split("\n    ports:", 1)[0]
        self.assertIn("      - /etc/section9/sandbox.env", sandbox)     # still required
        self.assertTrue(re.search(r"- path: /etc/section9/codescene\.env\n\s+required: false$", sandbox), sandbox)

    def test_the_entrypoint_stores_the_token_for_the_agent_only_and_drops_it(self):
        text = self.read("sandbox/entrypoint.sh")
        stored = text.index('printf \'%s\\n\' "$CS_ACCESS_TOKEN" > "$home/.config/codescene/token"')
        self.assertIn("umask 077", text[text.index("unset GH_TOKEN"):stored])
        self.assertLess(stored, text.index("unset CS_ACCESS_TOKEN"))
        self.assertLess(text.index("unset CS_ACCESS_TOKEN"), text.index("exec /usr/sbin/sshd"))
