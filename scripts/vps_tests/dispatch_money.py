"""What the dispatcher counts: spend, the run ledger, CPU credits, pacing, day caps and the Capacity issue."""

import datetime as dt
import json
import tempfile
import tomllib
import unittest
from unittest import mock
from pathlib import Path

from .base import ROOT, ROSTER, Base, dispatcher


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

    def test_the_cash_multiplier_is_shown_alongside_usage(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("cash_multiplier = 1.32\n")
        self.run_dispatch("spend", limits=limits)
        (n, issue), = self.stub.issues.items()
        self.assertIn("This month (UTC): $5.00", issue["body"])
        self.assertIn("$6.60", issue["body"])           # cash = 5.00 x 1.32
        self.assertIn("Cash (x1.32", issue["body"])

    def seed_ledger(self, rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc)
        with path.open("w", encoding="utf-8") as f:
            for agent, cost in rows:
                f.write(json.dumps({"agent": agent, "start": now.isoformat(), "outcome": "ok",
                                    "cost_usd": cost}) + "\n")

    def spend_body(self):
        self.run_dispatch("spend")
        (issue,) = self.stub.issues.values()
        return issue["body"]

    def test_reconciliation_books_the_gap_to_hermes_background(self):
        self.seed_ledger([("Batou", 1.0), ("Togusa", 0.5), ("Batou", 0.25), ("Saito", 0.75)])
        body = self.spend_body()
        self.assertIn("Booked to agents (ledger, this month): $2.50", body)
        self.assertIn("Batou $1.25 \u00b7 Togusa $0.50 \u00b7 Tachikoma $0.00 \u00b7 Ishikawa $0.00 \u00b7 Saito $0.75", body)
        self.assertIn("Hermes background (not tied to a run): $2.50", body)
        self.assertIn("Owned total: $5.00 (= key usage)", body)
        self.assertIn("This month (UTC): $5.00", body)

    def test_reconciliation_when_the_ledger_exceeds_the_key(self):
        self.seed_ledger([("Ishikawa", 7.0)])
        body = self.spend_body()
        self.assertIn("Hermes background (not tied to a run): $0.00 (ledger exceeds key", body)
        self.assertIn("two-lane overlap", body)
        self.assertIn("Owned total: $7.00 (key usage $5.00)", body)

    def test_reconciliation_with_an_empty_ledger(self):
        body = self.spend_body()
        self.assertIn("Booked to agents (ledger, this month): $0.00", body)
        self.assertIn("Batou $0.00 \u00b7 Togusa $0.00 \u00b7 Tachikoma $0.00 \u00b7 Ishikawa $0.00", body)
        self.assertIn("Hermes background (not tied to a run): $5.00", body)
        self.assertIn("Owned total: $5.00 (= key usage)", body)

    def test_dispatcher_ignores_it(self):
        self.run_dispatch("spend")
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])


class TestCredits(Base):
    def test_one_issue_with_steal_the_ceiling_and_the_caps_no_agent_labels(self):
        with mock.patch.object(dispatcher.cpu, "SAMPLE_SECONDS", 0.01), \
                mock.patch.object(dispatcher.cpu, "PROBE_SECONDS", 0.01):
            self.run_dispatch("cpu")
            self.run_dispatch("cpu")
        (n, issue), = self.stub.issues.items()
        self.assertEqual((issue["title"], self.stub.labels(n)), ("CPU credits", set()))
        for line in ("Limit:", "Steal:", "Busy:", "Caps in force:", "As of "):
            self.assertIn(line, issue["body"])
        self.assertEqual(self.stub.todo_count(), 0)      # a snapshot, not a task


class TestLedger(Base):
    def ledger(self):
        path = self.t / "state" / "runs.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_one_json_line_per_run_with_agent_type_tokens_cost_and_cpu(self):
        self.stub.bump = 0.03
        n = self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.assertEqual(self.run_dispatch()[0], 0)
        (row,) = self.ledger()
        # The model is what the run asked for (the gateway default here), plus the model it was booked to.
        self.assertEqual((row["v"], row["issue"], row["agent"], row["role"], row["type"], row["model"]),
                         (1, n, "Batou", "", "qa", dispatcher.limits.DEFAULT_MODEL))
        self.assertEqual(row["model_requested"], dispatcher.limits.DEFAULT_MODEL)
        self.assertEqual((row["outcome"], row["queue_before"]), ("ok", 1))
        self.assertGreaterEqual(row["seconds"], 0)
        self.assertEqual(row["cost_usd"], 0.03)
        self.assertEqual(row["usage_before"]["usage_monthly"], 5.0)
        self.assertEqual(row["usage_after"]["usage_monthly"], 5.03)
        self.assertEqual(row["tokens"], {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        self.assertEqual(set(row["cpu"]), {"wall_seconds", "steal_pct", "core_seconds", "credits"})
        self.assertEqual(set(row["cpu_before"]), {"at", "busy", "steal", "total"})
        self.assertGreaterEqual(row["cpu_after"]["total"], row["cpu_before"]["total"])
        self.assertGreaterEqual(row["cpu"]["core_seconds"], 0)
        self.assertTrue(row["start"].endswith("+00:00") and row["end"].endswith("+00:00"))

    def test_outcomes_are_named_not_guessed(self):
        cases = (("silent", "no-output"), ("error", "blocked"))
        for mode, expected in cases:
            with self.subTest(mode=mode):
                self.setUp()
                self.stub.hermes = mode
                self.stub.add(["agent:batou", "status:todo"])
                self.run_dispatch()
                self.assertEqual(self.ledger()[0]["outcome"], expected)
                self.tearDown()

    def test_a_timeout_is_recorded_as_a_timeout_with_its_hours(self):
        (self.t / "roster.toml").write_text(ROSTER.format(timeout=1))
        self.stub.hermes = "slow"
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        row = self.ledger()[0]
        self.assertEqual(row["outcome"], "timeout")
        self.assertGreaterEqual(row["seconds"], 1.0)      # the 1 s run ceiling ends it; the record says so

    def test_a_broken_ledger_never_stops_the_run(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatcher.ledger, "append_ledger", side_effect=OSError("disk full")):
            code, out = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        self.assertIn("ledger: not written", out)

    def test_an_issue_without_a_readable_cost_is_not_given_one(self):
        self.stub.key = {"data": {"usage": 5, "limit": 20, "limit_remaining": 15,
                                  "usage_monthly": None, "usage_daily": 0.25}}
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        row = self.ledger()[0]
        self.assertIsNone(row["cost_usd"])
        self.assertEqual(row["usage_before"], {"usage": 5, "usage_monthly": None, "usage_daily": 0.25,
                                               "limit": 20, "limit_remaining": 15})

    def test_a_failing_key_read_still_runs_and_records_no_cost(self):
        self.stub.key_fail_after = 1        # spend_ok's read succeeds; the ledger's two reads fail
        n = self.stub.add(["agent:batou", "status:todo"])
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        row = self.ledger()[0]
        self.assertEqual((row["usage_before"], row["usage_after"], row["cost_usd"]), ({}, {}, None))

    def test_key_usage_is_empty_when_the_read_fails(self):
        for boom in (dispatcher.net.Failure("no key"), ValueError("not JSON after all")):
            with self.subTest(boom=type(boom).__name__):
                with mock.patch.object(dispatcher.net, "http", side_effect=boom):
                    self.assertEqual(dispatcher.openrouter.key_usage("http://x/api/v1", "k"), {})

    def test_a_failing_cpu_sample_still_runs_and_records_no_cpu(self):
        n = self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatcher.cpu, "cpu_sample", side_effect=OSError("no /proc/stat")):
            code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})
        row = self.ledger()[0]
        self.assertEqual((row["cpu"], row["cpu_before"], row["cpu_after"], row["outcome"]),
                         (None, None, None, "ok"))

    def test_the_ledger_file_is_not_world_readable(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        mode = (self.t / "state" / "runs.jsonl").stat().st_mode & 0o777
        self.assertEqual(mode, 0o640)

    def test_the_ledger_writer_fixes_an_existing_files_mode(self):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"start": "old"}\n')
        path.chmod(0o644)                       # an older build (or a hand edit) left it world-readable
        self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(path.read_text().splitlines()[0], '{"start": "old"}')   # and it appended, not rewrote


class TestCpuUnits(unittest.TestCase):
    def test_proc_stat_ticks_are_converted_to_seconds(self):
        fields = [100, 0, 50, 900, 20, 10, 10, 5]        # user nice system idle iowait irq softirq steal
        with mock.patch.object(dispatcher.cpu, "CLK_TCK", 100.0):
            self.assertAlmostEqual(dispatcher.cpu.busy_seconds(fields), 1.7)   # (100+50+10+10)/100
            self.assertEqual(dispatcher.cpu.cpu_sample.__name__, "cpu_sample")

    def test_the_draw_is_not_a_hundred_times_too_big(self):
        before = {"at": 0.0, "busy": 0.0, "steal": 0.0, "total": 0.0}
        after = {"at": 3600.0, "busy": 1185.0, "steal": 18.0, "total": 3600.0}
        d = dispatcher.cpu.cpu_delta(before, after)
        self.assertAlmostEqual(d["credits"], 285.0)      # 1185 - 25% baseline x 3600 = 285
        self.assertAlmostEqual(d["steal_pct"], 0.5)


class TestPace(unittest.TestCase):
    def test_the_two_lines_for_a_forty_dollar_month(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        p = dispatcher.openrouter.month_pace(now, 40, 20)
        self.assertEqual(p["days"], 30)
        self.assertAlmostEqual(p["linear_per_day"], 39 / 30)
        self.assertAlmostEqual(p["linear_to_date"], (39 / 30) * 14.5)
        self.assertAlmostEqual(p["remaining_per_day"], (40 - 20 - 1) / 15.5)
        self.assertFalse(dispatcher.openrouter.over_pace(now, 40, 20))
        self.assertTrue(dispatcher.openrouter.over_pace(now, 40, 30))

    def test_slack_days_is_the_tolerance(self):
        now = dt.datetime(2026, 11, 15, 12, tzinfo=dt.timezone.utc)
        pace = dispatcher.openrouter.month_pace(now, 40, 20)["linear_to_date"]
        just_over = pace + 0.5 * dispatcher.openrouter.month_pace(now, 40, 20)["linear_per_day"]
        self.assertFalse(dispatcher.openrouter.over_pace(now, 40, just_over, slack_days=1.0))
        self.assertTrue(dispatcher.openrouter.over_pace(now, 40, just_over, slack_days=0.0))


class TestCash(unittest.TestCase):
    def test_the_multiplier_defaults_to_one_and_reads_a_number(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "limits.toml"
            self.assertEqual(dispatcher.limits.cash_multiplier(p), 1.0)          # absent file
            p.write_text("cash_multiplier = 1.32\n")
            self.assertEqual(dispatcher.limits.cash_multiplier(p), 1.32)
            p.write_text("cash_multiplier = \"lots\"\n")                # not a number
            self.assertEqual(dispatcher.limits.cash_multiplier(p), 1.0)
            p.write_text("lanes = 2\n")
            self.assertEqual(dispatcher.limits.cash_multiplier(p), 1.0)


class TestLimitsExample(unittest.TestCase):
    """The two follow-ups meet in /etc/section9/limits.toml: #100's top-level cash_multiplier and #101's
    [models] and [spend] tables. A top-level key placed after a table would silently become part of it."""

    def runbook_limits(self):
        text = (ROOT / "docs" / "RUNBOOK.md").read_text(encoding="utf-8")
        marker = "# /etc/section9/limits.toml"
        start = text.index(marker)
        return tomllib.loads(text[start:text.index("```", start)])

    def test_the_runbook_example_parses_with_every_key_where_the_dispatcher_reads_it(self):
        data = self.runbook_limits()
        self.assertEqual(data["cash_multiplier"], 1.266)             # top level, not inside [timeout_sec]
        self.assertNotIn("cash_multiplier", data["timeout_sec"])
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "limits.toml"
            p.write_text((ROOT / "docs" / "RUNBOOK.md").read_text(
                encoding="utf-8").split("# /etc/section9/limits.toml", 1)[1].split("```", 1)[0].split("\n", 1)[1])
            self.assertEqual(dispatcher.limits.cash_multiplier(p), 1.266)
            self.assertEqual(dispatcher.limits.spend_caps(p), {"Ishikawa": 8.0, "Tachikoma": 5.0})
            pins, allow = dispatcher.limits.models_of(p)
            self.assertIn("anthropic/claude-sonnet-5.5", allow)
            self.assertEqual(pins, {})

    def test_the_allow_list_in_the_example_holds_no_xai_model(self):
        self.assertTrue(all(dispatcher.limits.model_ok(m) for m in self.runbook_limits()["models"]["allow"]))


class TestPacing(Base):
    def limits(self, body):
        path = self.t / "limits.toml"
        path.write_text(body)
        return str(path)

    # The month's pace line as month_pace gives it; a test sets `linear_to_date` (spend is $5 in the stub).
    PACE = {"linear_per_day": 1.0, "elapsed": 1, "days": 30, "left_days": 29, "remaining_per_day": 1}
    ON = "[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n"

    def run_paced(self, to_date, limits=ON, cpu=None):
        """One tick with the pace line at `to_date` dollars. The CPU reads no steal, unless `cpu`
        (a patch) says otherwise; `limits` None means no limits file at all."""
        cpu = cpu or mock.patch.object(dispatcher.cpu, "cpu_delta", return_value={"steal_pct": 0.0})
        pace = mock.patch.object(dispatcher.openrouter, "month_pace",
                                 return_value=dict(self.PACE, linear_to_date=to_date))
        with pace, cpu:
            return self.run_dispatch(limits=self.limits(limits) if limits else None)

    def assert_held(self, result, low, why="holding 1 low-priority"):
        """The low-priority task still waits, Ishikawa's ordinary one ran, and the log says why."""
        code, out = result
        self.assertEqual(code, 0)
        self.assertIn("status:todo", self.stub.labels(low))
        self.assertEqual(self.stub.labels(self.normal), {"agent:ishikawa", "status:review"})
        self.assertIn(why, out)

    def queue(self, label="priority:low"):
        """A low-priority task for Batou and an ordinary one for Ishikawa. Returns the low one."""
        low = self.stub.add(["agent:batou", "status:todo", label])
        self.normal = self.stub.add(["agent:ishikawa", "status:todo"])
        return low

    def test_on_by_default_holds_a_low_priority_task_over_the_line(self):
        # #205 item 4: pacing is a code default -- no limits file names it, and a low task is held.
        low = self.queue()
        self.assert_held(self.run_paced(0.0, limits=None), low)          # no limits file at all

    def test_a_root_file_can_turn_pacing_off(self):
        # The one opt-out: `[pacing] enabled = false` in the root-only file.
        n = self.stub.add(["agent:batou", "status:todo", "priority:low"])
        code, out = self.run_dispatch(limits=self.limits("[pacing]\nenabled = false\n"))
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review", "priority:low"})
        self.assertNotIn("holding", out)

    def test_pacing_is_a_no_op_when_no_low_priority_task_is_queued(self):
        # It can only hold low-priority work, so with none queued it reads no figures and samples no CPU.
        todo = [({"number": 1, "labels": [{"name": "status:todo"}]}, {"name": "Batou"}, False, None)]
        with mock.patch.object(dispatcher.limits, "pacing_settings") as settings, \
                mock.patch.object(dispatcher.openrouter, "key_usage") as usage:
            self.assertIs(dispatcher.pick.hold_low_priority(todo, None, None, lambda *a: None), todo)
        settings.assert_not_called()
        usage.assert_not_called()

    def test_over_pace_holds_only_low_priority_work(self):
        self.queue()
        result = self.run_paced(0.0)
        low = next(i for i in self.stub.issues.values() if "priority:low" in self.stub.labels(i["number"]))
        self.assert_held(result, low["number"])

    def test_over_pace_holds_a_low_priority_task_whatever_the_label_case(self):
        # The pick lowercases labels; the pacing hold must normalise the same way (was a raw compare).
        low = self.queue("priority:Low")
        self.assert_held(self.run_paced(0.0), low)

    def test_an_index_error_reading_the_cpu_holds_low_priority_not_crashes(self):
        # #78 nit 1: a short /proc/stat line (IndexError) must fail closed, not crash the tick.
        low = self.queue()
        short = mock.patch.object(dispatcher.cpu, "cpu_sample", side_effect=IndexError("short cpu line"))
        self.assert_held(self.run_paced(0.0, cpu=short), low, why="CPU figures unreadable")

    def test_under_pace_admits_the_low_priority_task_too(self):
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"])
        self.run_paced(100.0)
        self.assertEqual(self.stub.labels(low), {"agent:batou", "status:review", "priority:low"})


class TestDayCap(Base):
    """#205 item 6: a per-agent runs-per-day cap, a code default; it skips an agent, never blocks."""

    def seed_today(self, agent, count):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        now = dt.datetime.now(dt.timezone.utc).isoformat()
        with path.open("a", encoding="utf-8") as f:
            for _ in range(count):
                f.write(json.dumps({"agent": agent, "start": now, "outcome": "ok"}) + "\n")

    def test_an_agent_at_its_day_cap_is_skipped_and_another_runs(self):
        self.seed_today("Batou", 2)                        # the code default is 2 a day
        batou = self.stub.add(["agent:batou", "status:todo"], "Batou's task")
        ishi = self.stub.add(["agent:ishikawa", "status:todo"], "Ishikawa's task")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("Ishikawa's task", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(ishi), {"agent:ishikawa", "status:review"})
        self.assertEqual(self.stub.labels(batou), {"agent:batou", "status:todo"})   # waits, not blocked

    def test_the_only_todo_for_a_capped_agent_waits_and_is_logged(self):
        self.seed_today("Batou", 2)
        n = self.stub.add(["agent:batou", "status:todo"], "Batou's task")
        code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})       # not blocked
        self.assertIn("day cap", out)

    def test_a_zero_cap_never_skips_an_agent(self):
        # The nightly builder's roster entry says runs_per_day = 0: no day cap, however many runs.
        self.assertTrue(dispatcher.pick.day_ok(type("A", (), {"state_dir": str(self.t / "state"), "ledger": ""})(),
                                        {"name": "Tachikoma", "runs_per_day": 0}))

    def test_under_the_day_cap_the_agent_still_runs(self):
        self.seed_today("Batou", 1)                        # one of two used: a run is left
        n = self.stub.add(["agent:batou", "status:todo"], "Batou's task")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:review"})

    def test_a_pooled_task_avoids_agents_at_their_day_cap(self):
        for name in ("Batou", "Ishikawa", "Saito"):
            self.seed_today(name, 2)                       # the default cap: all three are done today
        n = self.stub.add(["agent:any", "status:todo"], "Anyone's task")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertIn("You are **Togusa**", self.stub.hermes_calls[0][0]["messages"][0]["content"])
        self.assertEqual(self.stub.labels(n), {"agent:togusa", "status:review"})

    def test_the_roster_cap_overrides_the_code_default(self):
        (self.t / "roster.toml").write_text(ROSTER.format(timeout=self.timeout).replace(
            'name = "Batou"\nbrief = "agents/coder.md"\ntimeout_sec = 60',
            'name = "Batou"\nbrief = "agents/coder.md"\ntimeout_sec = 60\nruns_per_day = 1'))
        self.seed_today("Batou", 1)                        # one run today, and the roster caps him at 1
        n = self.stub.add(["agent:batou", "status:todo"], "Batou's task")
        code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:todo"})
        self.assertIn("day cap", out)


class TestCapacity(Base):
    def capacity(self, limits=None):
        code, out = self.run_dispatch("capacity", limits=limits)
        self.assertEqual(code, 0, out)
        return out

    def test_the_cash_multiplier_shows_both_usage_and_cash(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("cash_multiplier = 1.32\n")
        self.stub.bump = 0.03
        self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.run_dispatch()
        self.capacity(limits=limits)
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("$5.03", body)            # usage, as OpenRouter reports it
        self.assertIn("$6.64", body)            # cash = 5.03 x 1.32
        self.assertIn("Cash (x1.32", body)

    def test_without_the_multiplier_there_is_no_cash_line(self):
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertNotIn("Cash (x", body)

    def test_creates_then_updates_one_pinned_issue_with_the_roll_up(self):
        self.stub.bump = 0.03
        self.stub.add(["agent:batou", "status:todo"], "QA: check the spacing")
        self.run_dispatch()
        self.capacity()
        (n, issue), = [x for x in self.stub.issues.items() if x[1]["title"] == "Capacity"]
        self.assertEqual((self.stub.labels(n), self.stub.pinned), (set(), f"node-{n}"))
        for line in ("**Pace:**", "Last 7 days", "Month to date", "by agent", "by task type",
                     "Queue idle", "CPU credits", "$0.03", "| Batou | 1 |"):
            self.assertIn(line, issue["body"])
        self.capacity()
        self.assertEqual(sum(i["title"] == "Capacity" for i in self.stub.issues.values()), 1)

    def test_an_empty_ledger_still_writes_the_issue_and_says_what_is_missing(self):
        self.capacity()
        (n, issue), = self.stub.issues.items()
        self.assertIn("No ledger data yet", issue["body"])
        for line in ("—", "no run had a readable cost delta"):
            self.assertIn(line, issue["body"])

    def test_runs_without_a_cost_are_not_averaged_into_a_fake_rate(self):
        self.capacity()                       # creates the issue and the shape
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "v": 1, "issue": 1, "type": "qa", "agent": "Batou", "role": "engineer",
            "model": "hermes-agent", "start": "2026-11-04T10:00:00+00:00",
            "end": "2026-11-04T10:30:00+00:00", "seconds": 1800, "outcome": "ok",
            "queue_before": 2, "usage_before": {}, "usage_after": {}, "cost_usd": None,
            "tokens": None, "cpu": {"credits": 100}}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("no run had a readable cost delta", body)
        self.assertNotIn("$0.00/h", body)

    def test_capacity_shows_the_key_month_to_date_beside_the_ledger(self):
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "v": 1, "issue": 1, "type": "qa", "agent": "Batou", "role": "engineer",
            "model": "deepseek/deepseek-v4.1-flash", "start": now, "end": now, "seconds": 600,
            "outcome": "ok", "queue_before": 1, "cost_usd": 0.0,       # the counter did not move
            "tokens": {"prompt_tokens": 0, "completion_tokens": 1_000_000},
            "cpu": {"credits": 1}}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("Key (OpenRouter `/key`), month to date: $5.00", body)   # the stub's usage_monthly
        self.assertIn("ledger books $0.19 over 1 run(s) (1 priced)", body)
        self.assertIn("$4.81 unattached to any run", body)
        self.assertNotIn("no cost figure", body)      # the $0 delta was priced from the run's tokens

    def test_a_corrupt_ledger_line_is_skipped_not_fatal(self):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not json\n" + json.dumps({"start": "2026-11-04T10:00:00+00:00", "end":
            "2026-11-04T10:10:00+00:00", "agent": "Batou", "type": "qa", "seconds": 600,
            "outcome": "ok", "cost_usd": 0.02, "cpu": {"credits": 10}, "queue_before": 1}) + "\n")
        self.capacity()
        body = next(i["body"] for i in self.stub.issues.values() if i["title"] == "Capacity")
        self.assertIn("Runs: 1", body)


class TestRunCost(unittest.TestCase):
    """#205 item 1: a $0 key delta is not a free run; price it from the run's tokens."""

    def test_a_zero_key_delta_is_priced_from_the_run_tokens(self):
        row = {"model": "deepseek/deepseek-v4.1-flash", "cost_usd": 0.0,
               "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}
        self.assertAlmostEqual(dispatcher.ledger.run_cost(row), (1000 * 0.05 + 1000 * 0.19) / 1_000_000.0)

    def test_a_measured_delta_still_wins_over_the_tokens(self):
        self.assertEqual(dispatcher.ledger.run_cost({"model": "deepseek/deepseek-v4.1-flash", "cost_usd": 0.25,
                                            "tokens": {"prompt_tokens": 1000, "completion_tokens": 1000}}),
                         0.25)

    def test_a_zero_delta_with_no_tokens_stays_zero_and_unpriced(self):
        self.assertEqual(dispatcher.ledger.run_cost({"cost_usd": 0.0, "tokens": None}), 0.0)
        self.assertFalse(dispatcher.ledger.priceable({"cost_usd": 0.0, "tokens": None}))
        self.assertTrue(dispatcher.ledger.priceable({"cost_usd": 0.0, "tokens": {"total_tokens": 1}}))
