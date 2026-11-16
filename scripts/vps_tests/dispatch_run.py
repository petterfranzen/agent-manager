"""The dispatcher's run: pick, claim, run through Hermes, and the guards around it."""

import contextlib
import datetime as dt
import fcntl
import io
import json
import os
import subprocess
import sys
from unittest import mock
from pathlib import Path

from .base import ROOT, ROSTER, Base, dispatch, dispatcher
from .stub import ORKEY, HKEY


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

    def test_a_picked_issue_that_is_no_longer_todo_is_not_claimed(self):
        # #78 should-fix 3: the todo list read may be stale; re-check the issue right before claiming.
        n = self.stub.add(["agent:batou", "status:todo"])
        def steal(todo, *args, **kwargs):
            self.stub.issues[n]["labels"] = [{"name": "agent:batou"}, {"name": "status:working"}]
            return todo
        with mock.patch.object(dispatcher.pick, "hold_low_priority", side_effect=steal):
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:working"})
        self.assertIn("no longer todo", out)

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
        with open(self.t / "state" / "dispatch.lane0.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            code, out = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (0, []))
        self.assertIn("in progress in lane 0", out)

    def test_status_reports_queue_and_hermes(self):
        self.stub.add(["agent:batou", "status:todo"])
        _, out = self.run_dispatch("status")
        self.assertIn("1 todo", out)
        self.assertIn("hermes: reachable", out)
        self.assertIn("lanes: 1", out)

    def test_refuses_plain_http_to_a_remote_host(self):
        argv = ["--secrets-dir", str(self.t / "secrets"), "--github", "http://example.com"]
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(dispatch.main(argv), 1)
        self.assertIn("refusing", err.getvalue())


class TestFailClosed(Base):
    def blocked(self, reason):
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn(reason, self.stub.comments[0][1])

    def test_a_key_without_a_cap_blocks(self):
        self.stub.key = {"data": {"usage": 5, "limit": None, "limit_remaining": None, "usage_monthly": 5}}
        self.blocked("no monthly cap")

    def test_an_empty_or_unexpected_key_reply_blocks(self):
        for reply in ({}, {"data": {}}, {"data": None}, {"error": "incident"}, [], {"data": "oops"}):
            with self.subTest(reply=reply):
                self.setUp()
                self.stub.key = reply
                self.blocked("empty or not what was expected")
                self.tearDown()

    def test_a_cap_with_neither_a_remaining_balance_nor_usage_blocks(self):
        self.stub.key = {"data": {"limit": 20, "usage_monthly": 5}}     # a cap, but no usage and no limit_remaining
        self.blocked("neither a remaining balance nor usage")

    def test_a_cap_that_is_not_a_number_blocks(self):
        self.stub.key = {"data": {"limit": "lots", "limit_remaining": None}}
        self.blocked("not a number")

    def test_status_says_why(self):
        self.stub.key = {"data": {"limit": None}}
        _, out = self.run_dispatch("status")
        self.assertIn("spend: The OpenRouter key has no monthly cap", out)


class TestRecovery(Base):
    timeout = 3600          # Batou: swept after 70 min

    def test_a_killed_run_is_blocked_with_a_comment_before_work_is_picked(self):
        stuck = self.stub.add(["agent:batou", "status:working"], age_min=71)
        slow = self.stub.add(["agent:ishikawa", "status:working"], age_min=11)      # its timeout is 60 s: 11 min is stale
        fresh = self.stub.add(["agent:batou", "status:working"], age_min=69)
        todo = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(stuck), {"agent:batou", "status:blocked"})
        self.assertEqual(self.stub.labels(slow), {"agent:ishikawa", "status:blocked"})
        self.assertEqual(self.stub.labels(fresh), {"agent:batou", "status:working"})
        self.assertEqual([n for n, _ in self.stub.comments], [stuck, slow])
        self.assertIn("probably killed", self.stub.comments[0][1])
        self.assertIn("71 min", self.stub.comments[0][1])
        self.assertEqual(self.stub.labels(todo), {"agent:batou", "status:review"})     # and the queue still ran

    def test_the_sweep_uses_the_root_only_ceiling_not_the_roster_value(self):
        limits = str(self.t / "limits.toml")
        Path(limits).write_text("[timeout_sec]\nBatou = 1800\n")      # the roster says 3600 s
        stuck = self.stub.add(["agent:batou", "status:working"], age_min=41)    # past 1800+600 s, under 3600+600
        self.run_dispatch("run", limits=limits)
        self.assertEqual(self.stub.labels(stuck), {"agent:batou", "status:blocked"})
        self.assertIn("41 min", self.stub.comments[0][1])

    def test_with_nothing_todo_it_still_sweeps(self):
        n = self.stub.add(["agent:saito", "status:working"], age_min=12)
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:saito", "status:blocked"})

    def test_an_issue_without_a_known_agent_gets_the_default_timeout(self):
        young, old = self.stub.add(["status:working"], age_min=69), self.stub.add(["status:working"], age_min=71)
        self.run_dispatch()
        self.assertEqual((self.stub.labels(young), self.stub.labels(old)), ({"status:working"}, {"status:blocked"}))

    def test_a_blocked_issue_is_not_swept_twice(self):
        self.stub.add(["agent:saito", "status:working"], age_min=12)
        self.run_dispatch()
        self.run_dispatch()
        self.assertEqual(len(self.stub.comments), 1)


class TestPaging(Base):
    def test_todo_follows_link_next_to_the_oldest_issue_on_a_later_page(self):
        self.stub.page_size = 2
        for _ in range(5):
            self.stub.add(["status:todo"])                       # nobody's: the dispatcher must read past them
        last = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(last), {"agent:batou", "status:review"})

    def test_status_counts_every_page(self):
        self.stub.page_size = 3
        for _ in range(7):
            self.stub.add(["agent:batou", "status:todo"])
        _, out = self.run_dispatch("status")
        self.assertIn("7 todo", out)

    def test_routines_and_spend_read_every_page(self):
        self.stub.page_size = 1
        for _ in range(3):
            self.stub.add([], title="other")
        self.run_dispatch("spend")
        self.run_dispatch("spend")
        self.assertEqual(sum(i["title"] == "Spend" for i in self.stub.issues.values()), 1)

    def test_refuses_to_run_when_there_are_too_many_pages(self):
        self.stub.page_size = 1
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.add(["agent:batou", "status:todo"])
        with mock.patch.object(dispatcher.board, "MAX_PAGES", 1):
            code, _ = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls, self.stub.comments), (1, [], []))

    def test_never_follows_a_next_link_to_another_host(self):
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.page_size = 1
        self.stub.add(["agent:batou", "status:todo"])
        self.stub.base = "http://127.0.0.2:1"            # the Link header points elsewhere
        code, _ = self.run_dispatch()
        self.assertEqual((code, self.stub.hermes_calls), (1, []))


class TestTimeout(Base):
    timeout = 1

    def test_timeout_keeps_the_partial_output(self):
        self.stub.hermes = "slow"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        comment = self.stub.comments[0][1]
        self.assertIn("timed out at 1 s", comment)
        self.assertIn("partial line 1", comment)          # what the run wrote before its time ran out
        self.assertIn("Set `status:todo` to try again", comment)


class TestGuardrails(Base):
    """The root-only limits file: what the dispatcher refuses to take from the repo."""

    def limits(self, text="", brief=None):
        (self.t / "limits.toml").write_text(text)
        if brief is not None:
            (self.t / "secrets" / "briefs").mkdir(exist_ok=True)
            (self.t / "secrets" / "briefs" / "Batou.md").write_text(brief)
        return str(self.t / "limits.toml")

    def system_message(self, limits):
        self.stub.add(["agent:batou", "status:todo"])
        code, _ = self.run_dispatch("run", limits=limits)
        self.assertEqual(code, 0)
        return self.stub.hermes_calls[0][0]["messages"][0]["content"]

    def test_the_preamble_and_a_locked_brief_win_over_the_repo(self):
        limits = self.limits('preamble = "Read-only outside your task."\n', brief="You are Batou, locked brief.")
        self.assertEqual(self.system_message(limits),
                         "Read-only outside your task.\n\n---\n\nYou are Batou, locked brief.")

    def test_without_a_limits_file_the_repo_brief_is_used_as_before(self):
        system = self.system_message(str(self.t / "none-limits.toml"))
        self.assertIn("You are **Batou**", system)
        self.assertNotIn("Read-only", system)

    def test_a_run_is_cut_off_at_the_root_only_ceiling_not_the_roster_value(self):
        limits = self.limits("[timeout_sec]\nBatou = 1\n")      # the roster says 60 s
        self.stub.hermes = "slow"
        n = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch("run", limits=limits)
        self.assertEqual(self.stub.labels(n), {"agent:batou", "status:blocked"})
        self.assertIn("timed out at 1 s", self.stub.comments[0][1])

    def test_the_ceiling_is_the_root_only_value_and_the_roster_can_only_lower_it(self):
        self.assertEqual(dispatcher.limits.ceiling({"name": "Batou", "timeout_sec": 3600}, {"Batou": 60}), 60)
        self.assertEqual(dispatcher.limits.ceiling({"name": "Ishikawa", "timeout_sec": 60}, {"default": 30}), 30)
        self.assertEqual(dispatcher.limits.ceiling({"name": "Saito", "timeout_sec": 60}, {}), 60)
        caps, preamble = dispatcher.limits.limits_of(self.t / "gone.toml")
        self.assertEqual((caps, preamble), ({}, ""))
        self.assertEqual(dispatcher.limits.limits_of(self.limits('preamble = "p"\n[timeout_sec]\ndefault = 5\n')),
                         ({"default": 5}, "p"))


class TestAnyAgent(Base):
    """agent:any and agent:<role>: the pool takes the task, never its author, least recent work first."""

    ROSTER = """
[[agents]]
name = "Batou"
brief = "agents/coder.md"
role = "engineer"
timeout_sec = 60

[[agents]]
name = "Ishikawa"
brief = "agents/researcher.md"
role = "researcher"
timeout_sec = 60

[[agents]]
name = "Togusa"
brief = "agents/backup-coordinator.md"
role = "general"
title = "QA and UI/UX reviewer"
timeout_sec = 60
"""

    def setUp(self):
        super().setUp()
        (self.t / "roster.toml").write_text(self.ROSTER)

    def worked(self, labels, stamp):
        n = self.stub.add(labels)
        self.stub.issues[n]["updated_at"] = stamp
        return n

    def system_and_user(self):
        body, auth, session = self.stub.hermes_calls[0]
        return body["messages"][0]["content"], body["messages"][1]["content"], session

    def test_any_takes_the_least_recently_active_agent_not_the_author(self):
        self.worked(["agent:batou", "status:done"], "2026-11-03T12:00:00Z")
        self.worked(["agent:togusa", "status:done"], "2026-11-03T10:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T09:00:00Z")
        n = self.stub.add(["agent:any", "status:todo", "author:batou"], "QA Batou's PR")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(len(self.stub.hermes_calls), 1)
        system, user, session = self.system_and_user()
        self.assertIn("You are **Ishikawa**", system)            # oldest activity, and not the author
        self.assertNotIn("You are **Batou**", system)
        self.assertIn(f"s9-task --as Ishikawa status {n}", user)
        self.assertEqual(session, f"issue-{n}")
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "author:batou", "status:review"})

    def test_any_skips_an_author_who_would_otherwise_be_oldest(self):
        self.worked(["agent:togusa", "status:done"], "2026-11-03T11:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T10:00:00Z")
        self.worked(["agent:batou", "status:done"], "2026-11-03T08:00:00Z")   # oldest, but wrote it
        n = self.stub.add(["agent:any", "status:todo", "author:batou"], "Review Batou's work")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        system = self.system_and_user()[0]
        self.assertIn("You are **Ishikawa**", system)
        self.assertNotIn("You are **Batou**", system)
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "author:batou", "status:review"})

    def test_an_author_named_in_the_body_is_also_excluded(self):
        n = self.stub.add(["agent:any", "status:todo"], "QA the PR")
        self.stub.issues[n]["body"] = "Author: Batou\n\nPlease review."
        self.run_dispatch()
        system = self.system_and_user()[0]
        self.assertNotIn("You are **Batou**", system)

    def test_a_named_agent_who_wrote_it_is_blocked_not_run(self):
        n = self.stub.add(["agent:batou", "status:todo", "author:batou"], "Review Batou's PR")
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "author:batou", "status:blocked"})
        self.assertIn("wrote it", self.stub.comments[0][1])
        self.assertIn("status:todo", self.stub.comments[0][1])

    def test_a_role_label_reaches_only_an_agent_with_that_role(self):
        n = self.stub.add(["agent:engineer", "status:todo", "author:ishikawa"], "Fix the bug")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("You are **Batou**", self.system_and_user()[0])
        self.assertEqual(self.stub.labels(n), {"agent:batou", "author:ishikawa", "status:review"})

    def test_a_role_with_only_its_author_blocks_instead_of_self_reviewing(self):
        n = self.stub.add(["agent:qa", "status:todo", "author:togusa"], "QA Togusa's own PR")
        self.run_dispatch()
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:qa", "author:togusa", "status:blocked"})
        self.assertIn("the author (togusa)", self.stub.comments[0][1])

    def test_oldest_any_task_still_goes_first(self):
        first = self.stub.add(["agent:any", "status:todo"], "First")
        second = self.stub.add(["agent:any", "status:todo"], "Second")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("First", self.system_and_user()[1])
        self.assertEqual(self.stub.labels(first), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(second), {"agent:any", "status:todo"})

    def test_a_named_task_is_still_taken_by_its_named_agent(self):
        n = self.stub.add(["agent:ishikawa", "status:todo"])
        self.run_dispatch()
        self.assertIn("You are **Ishikawa**", self.system_and_user()[0])
        self.assertEqual(self.stub.labels(n), {"agent:ishikawa", "status:review"})

    def test_pooled_task_is_recorded_in_the_ledger_under_the_agent_it_went_to(self):
        self.worked(["agent:batou", "status:done"], "2026-11-03T12:00:00Z")
        self.worked(["agent:ishikawa", "status:done"], "2026-11-03T11:00:00Z")
        self.worked(["agent:togusa", "status:done"], "2026-11-03T10:00:00Z")
        n = self.stub.add(["agent:any", "status:todo"])
        self.run_dispatch()
        (row,) = [json.loads(l) for l in (self.t / "state" / "runs.jsonl").read_text().splitlines()]
        self.assertEqual((row["issue"], row["agent"], row["outcome"]), (n, "Togusa", "ok"))

    def test_pacing_holds_a_low_priority_pooled_task_and_runs_the_next_one(self):
        low = self.stub.add(["agent:any", "status:todo", "priority:low"])
        normal = self.stub.add(["agent:ishikawa", "status:todo"])
        path = self.t / "limits.toml"
        path.write_text("[timeout_sec]\ndefault = 60\n[pacing]\nenabled = true\nsample_seconds = 0\n")
        with mock.patch.object(dispatcher.openrouter, "month_pace",
                               return_value={"linear_to_date": 0.0, "linear_per_day": 1.0, "elapsed": 1,
                                             "days": 30, "left_days": 29, "remaining_per_day": 1}), \
                mock.patch.object(dispatcher.cpu, "cpu_delta", return_value={"steal_pct": 0.0}):
            self.run_dispatch(limits=str(path))
        self.assertIn("status:todo", self.stub.labels(low))
        self.assertIn("agent:any", self.stub.labels(low))
        self.assertEqual(self.stub.labels(normal), {"agent:ishikawa", "status:review"})


class TestPriority(Base):
    def test_a_high_priority_bug_is_picked_before_an_older_ordinary_task(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old feature request")
        bug = self.stub.add(["agent:batou", "status:todo", "priority:high"], "Fix the broken board")
        self.run_dispatch()
        self.assertEqual(len(self.stub.hermes_calls), 1)
        self.assertIn("Fix the broken board", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(bug), {"agent:batou", "priority:high", "status:review"})
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:todo"})

    def test_a_low_priority_task_waits_behind_an_older_ordinary_one(self):
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"], "Nice to have, older")
        normal = self.stub.add(["agent:batou", "status:todo"], "Ordinary, newer")
        self.run_dispatch()
        self.assertIn("Ordinary, newer", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(normal), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(low), {"agent:batou", "priority:low", "status:todo"})

    def test_without_priority_labels_the_oldest_is_still_picked(self):
        self.stub.add(["agent:batou", "status:todo"], "First")
        self.stub.add(["agent:batou", "status:todo"], "Second")
        self.run_dispatch()
        self.assertIn("First", self.stub.hermes_calls[0][0]["messages"][1]["content"])

    def test_priority_picks_the_issue_and_agent_any_still_picks_the_agent(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old ordinary")
        bug = self.stub.add(["agent:any", "status:todo", "priority:high"], "New high bug")
        self.run_dispatch()
        self.assertIn("New high bug", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        labels = self.stub.labels(bug)
        self.assertIn("status:review", labels)
        self.assertTrue(any(l.startswith("agent:") and l != "agent:any" for l in labels))
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:todo"})

    def test_an_issue_with_two_agent_labels_chooses_the_same_one_under_any_hash_seed(self):
        lines = [
            "import importlib.util, sys",
            "spec = importlib.util.spec_from_file_location('d', sys.argv[1])",
            "m = importlib.util.module_from_spec(spec)",
            "spec.loader.exec_module(m)",
            "issue = {'labels': [{'name': 'agent:any'}, {'name': 'agent:qa'}], 'body': ''}",
            "agents = [{'name': 'A'}, {'name': 'B', 'role': 'qa'}, {'name': 'C', 'title': 'QA reviewer'}]",
            "print(len(m.board.candidates(issue, agents)[0]))",
        ]
        seen = set()
        for seed in ("0", "3", "12345", "random"):        # seed 3 exposes the old set-order pick
            env = dict(os.environ, PYTHONHASHSEED=seed)
            out = subprocess.run([sys.executable, "-c", "\n".join(lines), str(ROOT / "vps" / "dispatch.py")],
                                 capture_output=True, text=True, env=env)
            seen.add(out.stdout.strip())
        self.assertEqual(seen, {"3"})        # 'any' wins the tie, so all three agents, delete-proof

    def test_an_unknown_agent_label_is_blocked_when_it_is_the_only_todo(self):
        n = self.stub.add(["agent:ghost", "status:todo"])
        code, _ = self.run_dispatch()
        self.assertEqual(code, 0)
        self.assertEqual(self.stub.hermes_calls, [])
        self.assertEqual(self.stub.labels(n), {"agent:ghost", "status:blocked"})
        self.assertIn("no agent has the role", self.stub.comments[0][1])

    def test_an_unknown_agent_label_is_left_alone_while_other_work_runs(self):
        bad = self.stub.add(["agent:ghost", "status:todo"])
        good = self.stub.add(["agent:batou", "status:todo"])
        self.run_dispatch()
        self.assertEqual(self.stub.labels(bad), {"agent:ghost", "status:todo"})
        self.assertEqual(self.stub.labels(good), {"agent:batou", "status:review"})

    def test_priority_rank_ages_a_waiting_task_up_one_level(self):
        # Direct, so the rule is pinned: a task over 12 h rises one level, a low one over 48 h tops out.
        now = dt.datetime(2026, 11, 5, 12, tzinfo=dt.timezone.utc)
        def rank(labels, ago_h):
            issue = {"labels": [{"name": x} for x in labels],
                     "created_at": (now - dt.timedelta(hours=ago_h)).isoformat()}
            return dispatcher.board.priority_rank(issue, now)
        self.assertEqual(rank([], 0), 1)
        self.assertEqual(rank(["priority:high"], 0), 0)
        self.assertEqual(rank(["priority:Low"], 0), 2)         # label case never matters
        self.assertEqual(rank(["Priority:HIGH"], 0), 0)
        self.assertEqual(rank([], 5), 1)                       # under the line: unchanged
        self.assertEqual(rank([], 13), 0)                      # an old ordinary joins the high group
        self.assertEqual(rank(["priority:low"], 13), 1)        # a low one rises one level
        self.assertEqual(rank(["priority:low"], 47), 1)
        self.assertEqual(rank(["priority:low"], 49), 0)        # over 48 h it tops out
        self.assertEqual(rank(["priority:high"], 49), 0)       # already at the top

    def test_a_starving_ordinary_task_overtakes_a_fresh_high_task(self):
        old = self.stub.add(["agent:batou", "status:todo"], "Old ordinary, waiting", age_min=13 * 60)
        bug = self.stub.add(["agent:batou", "status:todo", "priority:high"], "Fresh high bug")
        self.run_dispatch()
        self.assertIn("Old ordinary, waiting", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(old), {"agent:batou", "status:review"})
        self.assertEqual(self.stub.labels(bug), {"agent:batou", "priority:high", "status:todo"})

    def test_a_low_task_over_48h_overtakes_a_high_task(self):
        # Pacing off (#205 item 4 turns it on by default), so this measures the aging rule alone.
        limits = self.t / "limits.toml"
        limits.write_text("[pacing]\nenabled = false\n")
        low = self.stub.add(["agent:batou", "status:todo", "priority:low"], "Low, waiting 49 h", age_min=49 * 60)
        high = self.stub.add(["agent:batou", "status:todo", "priority:high"], "High, fresh")
        self.run_dispatch(limits=str(limits))
        self.assertIn("Low, waiting 49 h", self.stub.hermes_calls[0][0]["messages"][1]["content"])
        self.assertEqual(self.stub.labels(low), {"agent:batou", "priority:low", "status:review"})
        self.assertEqual(self.stub.labels(high), {"agent:batou", "priority:high", "status:todo"})
