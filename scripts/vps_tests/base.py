"""What the VPS tests share: the scripts under test, loaded by path, and a TestCase with a stub server."""

import contextlib
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path

from .stub import GH, HKEY, ORKEY, Stub, serve

ROOT = Path(__file__).resolve().parent.parent.parent


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / "vps" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dispatch = load("dispatch", "dispatch.py")     # also puts vps/ on sys.path, for its parts:
import dispatcher  # noqa: E402,F401
hq = load("hq_status", "hq-status.py")
sca = load("sca_watch", "sca-watch.py")

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
brief = "agents/researcher.md"
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
brief = "agents/researcher.md"
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
