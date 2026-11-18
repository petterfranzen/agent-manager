"""The second lane, per-agent models, the roster's timeouts and the cron routines."""

import datetime as dt
import fcntl
import json
import subprocess
import sys
import time
import tomllib
import unittest
from unittest import mock

from .base import ROOT, ROSTER_MODEL, Base, dispatcher
from .stub import GH, ORKEY, HKEY


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

    def test_a_routine_issue_in_review_does_not_block_tonights_run(self):
        # The nightly build's old issue stays open while its PR is in QA; that must not skip the next night.
        old = self.stub.add(["agent:saito", "status:review"], title="Morning report")
        blocked = self.stub.add(["agent:saito", "status:blocked"], title="Morning report")
        self.state(10)
        self.run_dispatch("routines")
        fresh = [n for n in self.stub.issues if n not in (old, blocked)]
        self.assertEqual(len(fresh), 1)
        self.assertEqual(self.stub.labels(fresh[0]), {"agent:saito", "status:todo"})

    def test_a_routine_still_todo_or_working_blocks_a_duplicate(self):
        for state in ("status:todo", "status:working"):
            self.stub.issues.clear()
            self.stub.add(["agent:saito", state], title="Morning report")
            self.state(10)
            self.run_dispatch("routines")
            self.assertEqual(len(self.stub.issues), 1, state)

    def test_a_malformed_private_routines_file_is_logged_and_the_public_routines_still_run(self):
        self.state(10)
        priv = self.t / "private.toml"
        priv.write_text('[[routines]]\nagent = "Saito"\ntitle = "SECRET-TITLE-9"\ncron = = oops\n')
        code, out = self.run_dispatch("routines", private=str(priv))
        self.assertEqual(code, 0)
        self.assertIn("private routines not loaded", out)
        self.assertNotIn("SECRET-TITLE-9", out)
        self.assertEqual([i["title"] for i in self.stub.issues.values()], ["Morning report"])

    def test_a_private_routine_missing_a_field_is_skipped_and_the_rest_run(self):
        self.state(10)
        priv = self.t / "private.toml"
        priv.write_text('[[routines]]\nagent = "Saito"\ntitle = "SECRET-TITLE-9"\n'
                        '[[routines]]\nagent = "Saito"\ntitle = "Private ok"\ncron = "* * * * *"\n')
        code, out = self.run_dispatch("routines", private=str(priv))
        self.assertEqual(code, 0)
        self.assertIn("private routine 1 skipped (check: cron)", out)
        self.assertNotIn("SECRET-TITLE-9", out)
        self.assertEqual(sorted(i["title"] for i in self.stub.issues.values()), ["Morning report", "Private ok"])

    def test_a_routine_can_claim_a_priority(self):
        roster = (self.t / "roster.toml")
        text = roster.read_text() if roster.exists() else None
        if text is None:
            self.skipTest("fixture roster is not a file here")
        roster.write_text(text.replace('title = "Morning report"', 'priority = "high"\ntitle = "Morning report"'))
        self.state(10)
        self.run_dispatch("routines")
        self.assertEqual([self.stub.labels(i["number"]) for i in self.stub.issues.values()],
                         [{"agent:saito", "status:todo", "priority:high"}])

    def test_first_run_does_not_backfill(self):
        self.run_dispatch("routines")
        self.assertEqual(self.stub.issues, {})

    def test_cron(self):
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Stockholm")
        at = lambda h, m, d=1: dt.datetime(2026, 11, d, h, m, tzinfo=tz)   # 1 Nov 2026 is a Sunday
        self.assertTrue(dispatcher.routines.cron_matches("40 5,16 * * *", at(16, 40)))
        self.assertFalse(dispatcher.routines.cron_matches("40 5,16 * * *", at(16, 41)))
        self.assertTrue(dispatcher.routines.cron_matches("0 1 * * 0", at(1, 0)))
        self.assertFalse(dispatcher.routines.cron_matches("0 1 * * 1-5", at(1, 0)))
        self.assertTrue(dispatcher.routines.cron_matches("0 1 * * 1-5", at(1, 0, 2)))
        self.assertTrue(dispatcher.routines.cron_matches("*/15 * * * *", at(3, 45)))


class TestLanes(Base):
    """#79: the second lane -- the lanes switch, what it may run beside lane 0, and the fallback."""

    TWO = "lanes = 2\n[timeout_sec]\ndefault = 60\n[pacing]\nsample_seconds = 0\n"

    def limits(self, text):
        path = self.t / "limits.toml"
        path.write_text(text)
        return str(path)

    def ledger(self):
        return [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]

    def latch(self):
        return json.loads((self.t / "state" / "lanes.json").read_text())

    def seed(self, rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def lane_one(self, limits=None):
        """Run lane 1 with the steal sample stubbed out: the tests seed the rest of the gate."""
        with mock.patch.object(dispatcher.cpu, "steal_secs", return_value=0.0):
            return self.run_dispatch("run", limits=limits or self.limits(self.TWO), lane=1)

    def test_lane_one_is_off_unless_the_switch_says_two(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        code, out = self.run_dispatch(lane=1)                      # no limits file at all: one lane
        self.assertEqual((code, self.stub.hermes_calls, out), (0, [], ""))   # quiet: no per-tick line while lanes = 1
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})
        code, out = self.run_dispatch(lane=1, limits=self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))

    def test_the_second_lane_runs_beside_lane_zero(self):
        busy = self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        mine = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        same_agent = self.stub.add(["agent:batou", "status:todo"], "portfolio: build direction B")
        code, out = self.lane_one()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("You are **Ishikawa**", self.stub.hermes_calls[0][0]["messages"][0]["content"])
        self.assertEqual(self.stub.labels(mine), {"agent:ishikawa", "status:review"})
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(same_agent), {"agent:batou", "status:todo"})
        self.assertEqual(self.stub.labels(busy), {"agent:batou", "status:working"})
        self.assertIn("(lane 1)", out)

    def test_the_second_lane_picks_by_priority_among_what_it_may_run(self):
        """#71 priority order and #79 lane filter together: the filter drops first, then priority picks."""
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        self.stub.add(["agent:batou", "status:todo", "priority:high"], "portfolio: urgent, but batou is busy")
        old = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: old ordinary")
        low = self.stub.add(["agent:saito", "status:todo", "priority:low"], "dinner-planner: nice to have")
        high = self.stub.add(["agent:saito", "status:todo", "priority:high"], "ghost-in-the-machine: broken")
        code, _ = self.lane_one()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(high), {"agent:saito", "status:review", "priority:high"})
        self.assertEqual(self.stub.labels(old), {"agent:ishikawa", "status:todo"})
        self.assertEqual(self.stub.labels(low), {"agent:saito", "status:todo", "priority:low"})
        self.assertEqual(self.ledger()[0]["lane"], 1)

    def test_the_second_lane_skips_a_task_whose_repo_it_cannot_tell(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        unknown = self.stub.add(["agent:ishikawa", "status:todo"], "Do a thing")
        known = self.stub.add(["agent:saito", "status:todo"], "nvim-trainer: add a keymap")
        code, _ = self.lane_one()
        self.assertEqual((code, self.stub.labels(unknown)), (0, {"agent:ishikawa", "status:todo"}))
        self.assertEqual(self.stub.labels(known), {"agent:saito", "status:review"})

    def test_an_unknown_repo_already_running_stands_the_second_lane_down(self):
        self.stub.add(["agent:batou", "status:working"], "Night build 2026-11-05 (Tachikoma)")
        self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        code, out = self.lane_one()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("found nothing", out)
        self.assertEqual(self.latch()["lanes"], 2)      # the reading was safe: two lanes stand

    def test_two_lanes_run_two_tasks_at_the_same_time(self):
        """Two real processes on the same state dir: lane 1 starts while lane 0 is in Hermes."""
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: fix the popping")
        second = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        limits = self.limits(self.TWO)
        self.stub.hermes = "slow-report"                # 2.5 s in Hermes: a real overlap
        def lane(n):
            argv = [sys.executable, str(ROOT / "vps" / "dispatch.py"), "run", "--lane", str(n),
                    "--secrets-dir", str(self.t / "secrets"), "--state-dir", str(self.t / "state"),
                    "--roster", str(self.t / "roster.toml"), "--private-routines", str(self.t / "none.toml"),
                    "--limits", limits, "--github", self.url + "/gh", "--openrouter", self.url + "/or",
                    "--hermes", self.url + "/hermes"]
            return subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        zero = lane(0)
        time.sleep(0.5)                                 # lane 0 is inside its Hermes call by now
        one = lane(1)
        out0, err0 = zero.communicate(timeout=60)
        out1, err1 = one.communicate(timeout=60)
        self.assertEqual((zero.returncode, one.returncode), (0, 0), err0 + err1)
        for text in (out0 + err0, out1 + err1):
            for secret in (GH, ORKEY, HKEY):
                self.assertNotIn(secret, text)
        self.assertEqual(sorted(session for _, _, session in self.stub.hermes_calls),
                         [f"issue-{first}", f"issue-{second}"])
        self.assertEqual([self.stub.labels(first), self.stub.labels(second)],
                         [{"agent:batou", "status:review"}, {"agent:ishikawa", "status:review"}])
        rows = self.ledger()
        self.assertEqual(sorted(r["lane"] for r in rows), [0, 1])
        self.assertGreater(dispatcher.ledger.concurrent_hours(rows), 0.0)
        self.assertIn("(lane 1)", out1)

    def unsafe_lane_one(self, **steal):
        """Lane 1 with a steal reading that is unsafe or unreadable: it stands down, leaves the task
        todo and writes the latch. Returns the log."""
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        with mock.patch.object(dispatcher.cpu, "steal_secs", **steal):
            code, out = self.run_dispatch("run", limits=self.limits(self.TWO), lane=1)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:todo"})
        self.assertEqual(self.latch()["lanes"], 1)
        return out

    def test_cpu_steal_drops_the_second_lane_and_writes_the_latch(self):
        out = self.unsafe_lane_one(return_value=25.0)
        self.assertIn("down to one lane", out)
        self.assertIn("steal 25.0%", out)
        self.assertIn("Hostinger", self.latch()["reason"])

    def test_unreadable_cpu_stands_the_second_lane_down_cleanly(self):
        out = self.unsafe_lane_one(side_effect=OSError("no /proc/stat"))
        self.assertIn("lanes: down to one lane: cpu unreadable (no /proc/stat)", out)

    def lane_zero(self, limits):
        with mock.patch.object(dispatcher.cpu, "steal_secs", return_value=0.0):
            return self.run_dispatch("run", limits=limits, lane=0)

    def test_lane_zero_skips_the_agent_lane_one_is_running(self):
        self.stub.add(["agent:batou", "status:working"], "portfolio: build direction B")
        same_agent = self.stub.add(["agent:batou", "status:todo"], "nvim-trainer: add a keymap")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_agent), {"agent:batou", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_zero_skips_the_repo_lane_one_is_working_in(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_zero_skips_an_unknown_repo_task_while_lane_one_runs(self):
        # #78 should-fix 1: while the other lane is running anything, an unknown-repo task conflicts too.
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        unknown = self.stub.add(["agent:ishikawa", "status:todo"], "Do a thing")
        code, _ = self.lane_zero(self.limits(self.TWO))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(unknown), {"agent:ishikawa", "status:todo"})

    def test_lane_zero_keeps_the_exclusion_while_lane_one_holds_its_run_lock(self):
        # #78 should-fix 2: lanes flipped back to 1 mid-run must not open the gap beside a live lane-1 run.
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        same_repo = self.stub.add(["agent:saito", "status:todo"], "flight-tracker: another pop")
        other = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        (self.t / "state").mkdir(exist_ok=True)
        lock = open(self.t / "state" / "dispatch.lane1.lock", "w")
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            code, _ = self.run_dispatch("run", limits=self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"), lane=0)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)
            lock.close()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(same_repo), {"agent:saito", "status:todo"})
        self.assertEqual(self.stub.labels(other), {"agent:ishikawa", "status:review"})

    def test_lane_one_stands_down_cleanly_on_an_index_error(self):
        # #78 nit 1: a short /proc/stat line must not crash the lane.
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        with mock.patch.object(dispatcher.cpu, "cpu_sample", side_effect=IndexError("short cpu line")):
            code, out = self.run_dispatch("run", limits=self.limits(self.TWO), lane=1)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:todo"})
        self.assertIn("cpu unreadable", out)
        self.assertEqual(self.latch()["lanes"], 1)

    def test_lane_zero_is_unchanged_with_one_lane(self):
        self.stub.add(["agent:batou", "status:working"], "flight-tracker: fix the popping")
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: another pop")
        code, _ = self.lane_zero(self.limits("lanes = 1\n[timeout_sec]\ndefault = 60\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(first), {"agent:batou", "status:review"})

    def test_the_credit_estimate_over_the_daily_guidance_drops_the_second_lane(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        at = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).isoformat(timespec="seconds")
        self.seed([{"start": at, "end": at, "agent": "Batou", "lane": 0, "cpu": {"credits": 9000}}])
        code, out = self.lane_one()
        self.assertEqual((code, self.stub.labels(n)), (0, {"agent:ishikawa", "status:todo"}))
        self.assertIn("9000 CPU credits", out)
        self.assertEqual(self.latch()["lanes"], 1)

    def test_the_second_lane_returns_only_after_the_quiet_period(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        limits = self.limits(self.TWO)
        latch = self.t / "state" / "lanes.json"
        latch.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda h: (now - dt.timedelta(hours=h)).isoformat(timespec="seconds")   # noqa: E731
        latch.write_text(json.dumps({"lanes": 1, "unsafe_at": stamp(0.5)}))
        code, out = self.lane_one(limits)
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("quiet period", out)
        latch.write_text(json.dumps({"lanes": 1, "unsafe_at": stamp(4)}))
        code, out = self.lane_one(limits)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:review"})
        self.assertIn("back to two lanes", out)
        self.assertEqual(self.latch()["lanes"], 2)

    def test_the_ledger_records_the_lane(self):
        first = self.stub.add(["agent:batou", "status:todo"], "flight-tracker: fix the popping")
        self.assertEqual(self.run_dispatch()[0], 0)
        second = self.stub.add(["agent:ishikawa", "status:todo"], "ghost-in-the-machine: research it")
        self.lane_one()
        self.assertEqual([(r["issue"], r["lane"]) for r in self.ledger()], [(first, 0), (second, 1)])

    def test_each_lane_has_its_own_run_lock(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"], "nvim-trainer: add a keymap")
        (self.t / "state").mkdir(exist_ok=True)
        with open(self.t / "state" / "dispatch.lane0.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, _ = self.lane_one()
        self.assertEqual((code, self.stub.labels(n)), (0, {"agent:ishikawa", "status:review"}))

    def test_one_lane_chooses_at_a_time(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        (self.t / "state").mkdir(exist_ok=True)
        with open(self.t / "state" / "dispatch.pick.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})
        self.assertIn("choosing", out)

    def test_capacity_reports_the_lanes(self):
        now = dt.datetime.now(dt.timezone.utc)
        stamp = lambda h: (now - dt.timedelta(hours=h)).isoformat(timespec="seconds")   # noqa: E731
        self.seed([{"start": stamp(3), "end": stamp(2), "agent": "Batou", "type": "qa", "lane": 0,
                    "seconds": 3600, "outcome": "ok", "cost_usd": 0.05, "cpu": {"credits": 100},
                    "queue_before": 2},
                   {"start": stamp(2.5), "end": stamp(1.5), "agent": "Ishikawa", "type": "research", "lane": 1,
                    "seconds": 3600, "outcome": "ok", "cost_usd": 0.04, "cpu": {"credits": 90},
                    "queue_before": 3}])
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("lane 0: 1 run(s), 1.00 h", body)
        self.assertIn("lane 1: 1 run(s), 1.00 h", body)
        self.assertIn("two at once 0.50 h", body)

    def test_repo_key_and_the_lanes_switch(self):
        self.assertEqual(dispatcher.board.repo_key({"title": "flight-tracker: x", "body": "", "labels": []}), "flight-tracker")
        self.assertEqual(dispatcher.board.repo_key({"title": "QA the PR", "body": "see nvim-trainer", "labels": []}),
                         "nvim-trainer")
        self.assertEqual(dispatcher.board.repo_key({"title": "Do a thing", "body": "", "labels": [{"name": "repo:portfolio"}]}),
                         "portfolio")
        self.assertEqual(dispatcher.board.repo_key({"title": "Do a thing", "body": "", "labels": []}), "")
        path = self.t / "limits.toml"
        for text, want in (("", 1), ("lanes = 1\n", 1), ("lanes = 2\n", 2), ("lanes = 3\n", 1),
                           ("lanes = '2'\n", 1)):
            path.write_text(text)
            self.assertEqual(dispatcher.limits.lanes_setting(path), want, text)

    def test_the_two_lanes_never_overlap(self):
        rows = [{"start": "2026-11-05T10:00:00+00:00", "end": "2026-11-05T11:00:00+00:00", "lane": 0},
                {"start": "2026-11-05T10:30:00+00:00", "end": "2026-11-05T11:30:00+00:00", "lane": 1},
                {"start": "2026-11-05T12:00:00+00:00", "end": "2026-11-05T12:10:00+00:00", "lane": 0}]
        self.assertAlmostEqual(dispatcher.ledger.concurrent_hours(rows), 0.5)
        self.assertTrue(dispatcher.capacity.lane_line(rows).endswith("two at once 0.50 h"))
        self.assertEqual(dispatcher.capacity.lane_line([]), "—")


class TestModels(Base):
    """#101: the per-agent model, the root-only allow-list and the per-agent spend guard."""

    def limits(self, body):
        path = self.t / "limits.toml"
        path.write_text(body)
        return str(path)

    def seed(self, rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def body_of(self, index=0):
        return self.stub.hermes_calls[index][0]

    def test_a_roster_model_in_the_allow_list_is_sent_with_the_provider(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual(body["model"], "anthropic/claude-sonnet-5.5")
        self.assertEqual(body["provider"], "openrouter")

    def test_a_roster_model_outside_the_allow_list_falls_back_to_the_default(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["deepseek/deepseek-v4.1-flash"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        code, out = self.run_dispatch(limits=limits)
        self.assertEqual((code, self.body_of()["model"]), (0, "hermes-agent"))
        self.assertIn("not in the root allow-list", out)

    def test_without_an_allow_list_the_roster_model_is_not_used(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch()                     # no limits file at all
        self.assertEqual(self.body_of()["model"], "hermes-agent")

    def test_the_default_request_is_the_model_the_messages_and_the_stream_flag(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()                     # no [models] table, roster names no model
        body = self.body_of()
        self.assertEqual(set(body), {"model", "messages", "stream"})
        self.assertEqual(body["model"], "hermes-agent")
        self.assertTrue(body["stream"])         # read as SSE, so a timeout keeps the partial answer
        self.assertNotIn("provider", body)
        self.assertEqual([m["role"] for m in body["messages"]], ["system", "user"])

    def test_a_default_resolved_model_also_streams(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\nSaito = "deepseek/deepseek-v4.1-flash"\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual((set(body), body["model"]), ({"model", "messages", "stream"}, "hermes-agent"))

    def test_the_saito_override_request_body_is_exact(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        limits = self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n')
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=limits)
        body = self.body_of()
        self.assertEqual(set(body), {"model", "provider", "messages", "stream"})
        self.assertEqual((body["model"], body["provider"]), ("anthropic/claude-sonnet-5.5", "openrouter"))

    def test_an_xai_model_is_refused_even_if_the_allow_list_names_it(self):
        pins, allow = dispatcher.limits.models_of(self.limits(
            '[models]\nallow = ["x-ai/grok-9-turbo"]\n'))
        self.assertEqual(dispatcher.limits.model_for({"name": "Saito", "model": "x-ai/grok-9-turbo"}, pins, allow),
                         dispatcher.limits.DEFAULT_MODEL)

    def test_the_root_pin_rolls_the_agent_back_with_one_line(self):
        pins, allow = dispatcher.limits.models_of(self.limits(
            '[models]\nallow = ["deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5"]\n'
            'Saito = "deepseek/deepseek-v4.1-flash"\n'))
        self.assertEqual(dispatcher.limits.model_for({"name": "Saito", "model": "anthropic/claude-sonnet-5.5"}, pins, allow),
                         dispatcher.limits.DEFAULT_MODEL)

    def test_the_ledger_records_the_model_that_answered(self):
        self.stub.hermes_runtime = {"provider": "openrouter", "model": "deepseek/deepseek-v4.1-flash",
                                    "route_source": "raw_request"}
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n'))
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(row["model"], "deepseek/deepseek-v4.1-flash")       # the served model, not the asked one
        self.assertEqual(row["model_requested"], "anthropic/claude-sonnet-5.5")

    def test_the_ledger_falls_back_to_the_reply_model_when_no_runtime_is_reported(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits('[models]\nallow = ["anthropic/claude-sonnet-5.5"]\n'))
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual(row["model"], row["model_requested"])

    def test_the_agent_is_blocked_when_its_monthly_budget_is_reached(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5", "cost_usd": 6.5}])
        n = self.stub.add(["agent:saito", "status:todo"])
        code, out = self.run_dispatch(limits=self.limits("[spend]\nSaito = 6.0\n"))
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:blocked"})
        self.assertIn("monthly budget", self.stub.comments[0][1])
        self.assertIn("$6.50", self.stub.comments[0][1])

    def test_a_run_under_the_budget_still_goes(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5", "cost_usd": 1.0}])
        n = self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits("[spend]\nSaito = 6.0\n"))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:review"})

    def test_an_agent_without_a_spend_entry_has_no_guard(self):
        (self.t / "roster.toml").write_text(ROSTER_MODEL)
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "agent": "Saito", "cost_usd": 99.0}])
        n = self.stub.add(["agent:saito", "status:todo"])
        self.run_dispatch(limits=self.limits("[spend]\nTachikoma = 6.0\n"))
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:review"})

    def test_the_guard_estimates_a_run_the_counter_could_not_price(self):
        self.assertAlmostEqual(dispatcher.ledger.run_cost({"model": "anthropic/claude-sonnet-5.5", "cost_usd": None,
                                                  "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}),
                               0.012)
        self.assertEqual(dispatcher.ledger.run_cost({"model": "who/knows", "tokens": {"completion_tokens": 1_000_000}}),
                         10.0)              # an unknown model is priced at the dearest allowed rate

    def test_capacity_shows_cost_by_agent_and_model(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        self.seed([{"start": now, "end": now, "agent": "Saito", "model": "anthropic/claude-sonnet-5.5",
                    "type": "review", "seconds": 600, "outcome": "ok", "cost_usd": 0.2, "cpu": {"credits": 10},
                    "queue_before": 1}])
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("by agent and model", body)
        self.assertIn("Saito \u00b7 anthropic/claude-sonnet-5.5", body)

    def test_capacity_lists_the_tasks_that_timed_out_with_their_cost(self):
        # Petter, 18 Nov: watch the jobs that cost money and never finish.
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        base = {"start": now, "end": now, "agent": "Batou", "model": "deepseek/deepseek-v4.1-flash",
                "type": "task", "queue_before": 1, "cpu": {"credits": 1}}
        self.seed([dict(base, issue=7, seconds=2400, outcome="timeout", cost_usd=0.30),
                   dict(base, issue=7, seconds=2400, outcome="timeout", cost_usd=0.20),
                   dict(base, issue=9, seconds=2400, outcome="timeout", cost_usd=0.10),
                   dict(base, issue=8, seconds=600, outcome="ok", cost_usd=0.40)])
        code, out = self.run_dispatch("capacity")
        self.assertEqual(code, 0, out)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("Timeouts: 3 of 4 runs (75%; 2.00 h and $0.60 wasted)", body)
        self.assertIn("| #7 | Batou | 2 | 80 | $0.50 |", body)
        self.assertLess(body.index("| #7 |"), body.index("| #9 |"))
        self.assertNotIn("| #8 |", body)


class TestRosterTimeouts(unittest.TestCase):
    """#205 items 2 and 6: the per-agent timeout and daily run cap, shipped in the roster."""

    def roster(self):
        return tomllib.loads((ROOT / "agents" / "roster.toml").read_text(encoding="utf-8"))

    def test_batou_and_togusa_default_to_2400_seconds(self):
        # #205 item 2: 16 timed-out runs in the week, Batou's 11 h and Togusa's 5 h, mostly stuck.
        timeouts = {a["name"]: a["timeout_sec"] for a in self.roster()["agents"]}
        self.assertEqual((timeouts["Batou"], timeouts["Togusa"]), (2400, 2400))

    def test_the_night_build_keeps_its_longer_run(self):
        # #218: the nightly build is never gated or shortened.
        timeouts = {a["name"]: a["timeout_sec"] for a in self.roster()["agents"]}
        self.assertEqual(timeouts["Tachikoma"], 10800)

    def test_every_agent_carries_its_daily_run_cap(self):
        # Tachikoma 0 = no cap: the nightly build is never gated (#218).
        caps = {a["name"]: a.get("runs_per_day") for a in self.roster()["agents"]}
        self.assertEqual(caps, {"Tachikoma": 0, "Batou": 6, "Ishikawa": 4, "Togusa": 8})
