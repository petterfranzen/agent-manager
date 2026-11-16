"""vps/hq-status.py and vps/sca-watch.py against the stub GitHub, OpenRouter, OSV and KEV."""

import contextlib
import io
import json
from unittest import mock

from .base import ROOT, Base, hq, sca
from .stub import GH, ORKEY


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
        self.stub.add(["agent:ishikawa", "status:done", "private"], "Morning ops report")

    def test_status_keeps_the_crew_format_and_hides_private_tasks(self):
        self.board()
        (self.t / "secrets" / "crew-deny.txt").write_text("# private topics\nsecret-garden\n")
        self.assertEqual(self.run_hq("status")[0], 0)
        doc = self.status()
        self.assertEqual(set(doc), {"generatedAt", "budget", "model", "agents", "tasks"})
        self.assertEqual(doc["budget"], {"usedPercent": 25, "month": doc["budget"]["month"]})     # 5 of 20, no dollars
        self.assertEqual(doc["model"], "deepseek/deepseek-v4.1-flash")
        self.assertEqual([a["name"] for a in doc["agents"]], ["Tachikoma", "Batou", "Ishikawa", "Togusa"])
        for a in doc["agents"]:
            self.assertEqual(set(a), {"name", "role", "state", "now", "lastActiveAt", "budgetPercent", "recent"})
        batou = self.agent(doc, "Batou")
        self.assertEqual((batou["state"], batou["now"]), ("working", "Fix the crew page layout"))
        self.assertEqual(batou["recent"], [{"at": "2026-11-01T10:00:00Z", "text": "Add dark mode",
                                            "url": "https://github.com/petterfranzen/ghost-in-the-machine/pull/12"}])
        self.assertEqual(self.agent(doc, "Ishikawa")["now"], "On a private task")       # labelled private
        self.assertEqual(self.agent(doc, "Togusa")["now"], "On a private task")         # deny term
        self.assertEqual(self.agent(doc, "Tachikoma")["state"], "idle")
        self.assertEqual(self.agent(doc, "Ishikawa")["recent"][0]["text"], "On a private task")
        self.assertNotIn("url", self.agent(doc, "Ishikawa")["recent"][0])
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

    def test_the_board_behind_access_shows_real_titles_while_the_root_copy_stays_redacted(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)          # no deny file at all
        root = self.status()
        self.assertEqual({t["title"] for t in root["tasks"]}, {"On a task", "On a private task"})
        for name in ("board.json", "status.json"):
            board = json.loads((self.t / "site" / "board" / name).read_text())
            titles = {t["title"] for t in board["tasks"]}
            self.assertIn("Fix the crew page layout", titles)
            self.assertIn("Look at private errand", titles)         # Petter sees his own private tasks
            self.assertNotIn("On a task", titles)
            fix = next(t for t in board["tasks"] if t["title"] == "Fix the crew page layout")
            self.assertTrue(fix["url"].startswith("https://github.com/petterfranzen/section9-hq/issues/"))
            self.assertEqual(self.agent(board, "Batou")["now"], "Fix the crew page layout")
            self.assertNotIn("$", json.dumps(board))                # still no money amounts
        raw = json.dumps(root)
        for leak in ("private errand", "crew page layout", "secret-garden"):
            self.assertNotIn(leak, raw)

    CAPACITY = (
        "# Capacity\n\nAs of 2026-11-13 09:05 UTC\n\n**Last 7 days by agent**\n\n"
        "| Agent | runs | busy h | $ | $/h | timeouts | timeout h | credits |\n|---|---|---|---|---|---|---|---|\n"
        "| Batou | 31 | 12.5 | $3.40 | $0.27 | 2 | 1.3 | ok |\n"
        "| Togusa | 9 | 2.0 | $0.55 | $0.28 | 0 | 0.0 | ok |\n\n"
        "**Last 7 days by agent and model**\n\n| Agent | Model | runs |\n|---|---|---|\n| Ishikawa | x | 99 |\n")

    def run_board(self, capacity=True):
        self.board()
        if capacity:
            self.stub.next = 80
            self.stub.issues[self.stub.add(["status:done"], "Capacity")]["body"] = self.CAPACITY
        self.stub.key = {"data": {"limit": 60, "limit_remaining": 23.756, "usage_monthly": 36.244, "usage_daily": 0.311}}
        self.assertEqual(self.run_hq("status")[0], 0)
        return json.loads((self.t / "site" / "board" / "board.json").read_text())

    def test_the_board_copy_carries_the_budget_in_dollars_and_each_agents_week(self):
        board = self.run_board()
        self.assertEqual(board["budget"], {"usedPercent": 60, "month": board["budget"]["month"], "usedUsd": 36.24,
                                           "capUsd": 60.0, "leftUsd": 23.76, "todayUsd": 0.31})
        self.assertEqual(board["capacityAsOf"], "2026-11-13T09:05:00Z")
        batou, tachi = self.agent(board, "Batou"), self.agent(board, "Tachikoma")
        self.assertEqual(batou["week"], {"runs": 31, "busyH": 12.5, "spendUsd": 3.4, "timeouts": 2})
        self.assertEqual((batou["runsPerDay"], batou["budgetMonthlyUsd"], batou["model"]), (6, 4.0, "DeepSeek (default)"))
        self.assertEqual((tachi["runsPerDay"], tachi["budgetMonthlyUsd"]), (0, 10.0))     # 0 = no run cap
        self.assertIsNone(tachi["week"])
        self.assertIsNone(self.agent(board, "Ishikawa")["week"])        # the by-model table is ignored

    def test_the_root_copy_gets_no_new_keys(self):
        self.run_board()
        root = self.status()
        self.assertEqual(set(root), {"generatedAt", "budget", "model", "agents", "tasks"})
        self.assertEqual(root["budget"], {"usedPercent": 60, "month": root["budget"]["month"]})
        for a in root["agents"]:
            self.assertEqual(set(a), {"name", "role", "state", "now", "lastActiveAt", "budgetPercent", "recent"})
        self.assertNotRegex(json.dumps(root), r"(?i)usd|\$|cents")

    def test_a_missing_capacity_issue_leaves_the_weeks_null_and_the_run_succeeds(self):
        board = self.run_board(capacity=False)          # the stub answers 404 for #80
        self.assertIsNone(board["capacityAsOf"])
        self.assertTrue(all(a["week"] is None for a in board["agents"]))
        self.assertEqual(board["budget"]["usedUsd"], 36.24)

    def test_a_key_without_amounts_gives_null_dollars(self):
        self.board()
        self.stub.key = {"data": {"usage_monthly": 5}}
        self.assertEqual(self.run_hq("status")[0], 0)
        budget = json.loads((self.t / "site" / "board" / "board.json").read_text())["budget"]
        self.assertEqual((budget["usedUsd"], budget["capUsd"], budget["leftUsd"], budget["todayUsd"]), (5.0, None, None, None))

    def test_without_a_deny_file_task_titles_are_not_published(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        self.assertEqual({t["title"] for t in self.status()["tasks"]}, {"On a task", "On a private task"})

    def test_status_writes_the_live_data_and_installs_the_vendored_page(self):
        self.board()
        self.assertEqual(self.run_hq("status")[0], 0)
        site = self.t / "site"
        board = (site / "board" / "board.json").read_text()                    # what the waits page polls
        self.assertEqual(set(json.loads(board)), set(json.loads((site / "status.json").read_text())) | {"capacityAsOf"})
        self.assertEqual((site / "board" / "status.json").read_text(), board)   # the placeholder's feed
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
        # docker-init (0.19.0) is the last `Version:` line in `docker version`; the server engine
        # version is what to query, so the --format call must win over the text scrape.
        def fake(cmd):
            if cmd[:3] == ["docker", "version", "--format"]:
                return "28.0.1\n"
            return {"docker": "Client:\n Version: 27.3.1\nServer:\n Engine:\n  Version: 28.0.1\n"
                              " containerd:\n  Version: 1.7.24\n runc:\n  Version: 1.1.12\n"
                              " docker-init:\n  Version: 0.19.0\n",
                    "runc": "runc version 1.1.12\ncommit: v1.1.12-0-g1d9c0e3\n",
                    "containerd": "containerd github.com/containerd/containerd v1.7.24 a88e0e0\n",
                    "dpkg": "Package: openssh-server\nSource: openssh\nVersion: 1:9.6p1-3ubuntu13\n"}[cmd[0]]
        with mock.patch.object(sca, "run", side_effect=fake):
            queries, _ = sca.host_pins()
        self.assertIn(("docker", "Go", "github.com/docker/docker", "28.0.1", "docker version"), queries)   # the server
        self.assertNotIn(("docker", "Go", "github.com/docker/docker", "0.19.0", "docker version"), queries)
        self.assertIn(("runc", "Go", "github.com/opencontainers/runc", "1.1.12", "runc --version"), queries)
        self.assertIn(("openssh-server", "Ubuntu", "openssh", "1:9.6p1-3ubuntu13", "dpkg -s openssh-server"), queries)

    def test_reads_the_server_engine_from_text_when_the_client_cannot_format(self):
        # A client too old for --format: the text scrape must still pick the engine, not docker-init.
        def fake(cmd):
            if cmd[0] != "docker":
                return ""
            if "--format" in cmd:
                return None
            return ("Server: Docker Engine - Community\n Engine:\n  Version: 29.9.2\n"
                    " containerd:\n  Version: 2.4.2\n runc:\n  Version: 1.5.2\n"
                    " docker-init:\n  Version: 0.19.0\n")
        with mock.patch.object(sca, "run", side_effect=fake):
            queries, _ = sca.host_pins()
        self.assertIn(("docker", "Go", "github.com/docker/docker", "29.9.2", "docker version"), queries)
        self.assertNotIn(("docker", "Go", "github.com/docker/docker", "0.19.0", "docker version"), queries)

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
