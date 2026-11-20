"""The "Code Health effect" issue: the run ledger joined with the PRs' `Code Health:` lines (#348)."""

import json
import unittest

from .base import Base, dispatcher
from .stub import GH

TITLE = "Code Health effect"
FT = "https://github.com/petterfranzen/flight-tracker/pull/"


def run(issue, outcome="ok", prompt=1000, **fields):
    """One ledger row of a coding run; `fields` overrides any other key (type, cost_usd, ...)."""
    row = {"v": 1, "issue": issue, "type": "task", "agent": "Batou", "model": "deepseek/deepseek-v4.1-flash",
           "start": "2026-11-18T10:00:00+00:00", "end": "2026-11-18T10:00:00+00:00", "seconds": 600,
           "outcome": outcome, "cost_usd": 0.01, "tokens": {"prompt_tokens": prompt, "completion_tokens": 0}}
    return {**row, **fields}


class TestCodeHealth(Base):
    def ledger(self, *rows):
        path = self.t / "state" / "runs.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in rows))

    def task(self, pr, pr_body, comments=(), state="closed"):
        """A finished board task whose last comment links a flight-tracker PR."""
        n = self.stub.add(["agent:batou", "status:done"], f"Task for PR {pr}", state=state)
        self.stub.issue_comments[n] = [f"PR: {FT}{pr}"]
        self.stub.prs[("flight-tracker", pr)] = {"body": pr_body, "comments": list(comments)}
        return n

    def body(self):
        code, out = self.run_dispatch("code-health")
        self.assertEqual(code, 0, out)
        (issue,) = [i for i in self.stub.issues.values() if i["title"] == TITLE]
        return issue["body"]

    def test_buckets_tasks_by_the_before_score_with_their_costs(self):
        low = self.task(7, "Code Health: frontend/src/main.ts 3.87 -> 4.10",
                        ["QA: changes needed", "Code Health: frontend/src/main.ts 3.87 -> 5.00\n\nQA: pass"])
        high = self.task(8, "Code Health: `src/a.ts` 9.8 -> 9.9\nCode Health: src/b.ts new -> 10.0")
        self.ledger(run(low, "timeout", prompt=300_000, cost_usd=0.10), run(low, prompt=100_000, cost_usd=0.05),
                    run(high, prompt=50_000, cost_usd=0.02), run(high, type="qa", prompt=9))
        body = self.body()
        self.assertIn("| low (under 7) (too few) | 1 | 400,000 | $0.15 | 100% | 100% | 100% |", body)
        self.assertIn("| high (9.5 and up) (too few) | 1 | 50,000 | $0.02 | 0% | 0% | 0% |", body)
        self.assertIn(f"| #{low} | flight-tracker#7 | 3.87 | 5.00 | 400,000 | $0.15 | 2 | timeout, fix, qa_fail |", body)
        self.assertIn(f"| #{high} | flight-tracker#8 | 9.80 | 9.95 |", body)    # the new file counts after only
        self.assertIn("too few tasks to conclude", body)

    def test_unfinished_tasks_and_qa_runs_are_left_out(self):
        open_ = self.task(9, "Code Health: x.ts 5.0 -> 6.0", state="open")
        self.stub.set_labels(open_, ["agent:batou", "status:review"])
        qa = self.task(10, "Code Health: y.ts 5.0 -> 6.0")
        self.ledger(run(open_), run(qa, type="qa"))
        body = self.body()
        self.assertIn("0 finished coding task(s)", body)
        self.assertIn("**Scored tasks:** none yet", body)

    def test_a_task_without_scores_or_with_only_new_files_has_its_own_row(self):
        bare = self.task(11, "No scores in this one.")
        new = self.task(12, "Code Health: z.ts new -> 9.7")
        self.ledger(run(bare), run(new))
        body = self.body()
        self.assertIn("| no scores (too few) | 1 |", body)
        self.assertIn("| new files only (too few) | 1 |", body)

    def test_enough_tasks_in_both_arms_gives_a_first_reading(self):
        rows = []
        for i in range(30):
            score = "9.8" if i % 2 else "5.0"
            rows.append(run(self.task(100 + i, f"Code Health: f{i}.ts {score} -> 9.9")))
        self.ledger(*rows)
        body = self.body()
        self.assertIn("enough for a first look** (15 tasks at 9.5 and up against 15)", body)
        self.assertIn("| high (9.5 and up) | 15 |", body)

    def test_an_unreadable_pr_is_skipped_and_counted_and_the_issue_is_kept_once(self):
        n = self.stub.add(["agent:batou", "status:done"], "Gone PR", state="closed")
        self.stub.issue_comments[n] = [f"{FT}404"]
        self.ledger(run(n))
        body = self.body()
        self.assertIn("1 task(s) or PR(s) could not be read", body)
        self.body()
        self.assertEqual(sum(i["title"] == TITLE for i in self.stub.issues.values()), 1)
        self.assertNotIn(GH, body)

    def test_an_empty_ledger_still_writes_the_issue(self):
        self.assertIn("too few tasks to conclude", self.body())


class TestCodeHealthParsing(unittest.TestCase):
    def test_score_lines_keep_the_first_before_and_the_last_after(self):
        files = {}
        dispatcher.health.read_scores(["- Code Health: a.py 7.5 → 8.0", "Code Health: a.py 6.0 -> 9.0"], files)
        self.assertEqual(files, {"a.py": {"before": 7.5, "after": 9.0}})

    def test_only_code_repos_count_as_pr_links(self):
        texts = ["see https://github.com/petterfranzen/section9-hq/pull/3 and "
                 "https://github.com/petterfranzen/nvim-trainer/pull/4 twice "
                 "https://github.com/petterfranzen/nvim-trainer/pull/4 and https://github.com/evil/x/pull/5"]
        self.assertEqual(dispatcher.health.pr_links(texts), [("nvim-trainer", 4)])

    def test_qa_verdict_is_the_last_line(self):
        self.assertTrue(dispatcher.health.qa_failed("notes\n**QA: changes needed**\n"))
        self.assertFalse(dispatcher.health.qa_failed("QA: changes needed?\nno, QA: pass"))
