"""Tests for deploy/heal-accounting.py against a stub Paperclip and a fake psql.

Run: python3 -m unittest scripts/test_heal_accounting.py
"""

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("heal", ROOT / "deploy" / "heal-accounting.py")
heal = importlib.util.module_from_spec(spec)
spec.loader.exec_module(heal)


def incident(iid, scope, limit, observed, threshold="hard", status="open"):
    return {"id": iid, "scopeType": "agent", "scopeName": scope, "scopeId": scope.lower(),
            "thresholdType": threshold, "status": status, "amountLimit": limit, "amountObserved": observed}


class StubApi:
    """Paperclip's resolve route: refuses when booked spend has reached the budget."""

    def __init__(self, incidents, spent):
        self.incidents = incidents
        self.spent = spent
        self.resolved = []

    def call(self, method, path, body=None):
        if path == "/api/companies":
            return [{"id": "c1", "name": "Other"}, {"id": "c9", "name": "Section 9"}]
        if path == "/api/companies/c9/budgets/overview":
            return {"activeIncidents": self.incidents}
        if method == "POST" and path.endswith("/resolve"):
            iid = path.split("/")[-2]
            inc = next(i for i in self.incidents if i["id"] == iid)
            if self.spent[iid] >= body["amount"]:
                raise heal.Failure(f"POST {path}: HTTP 422 New budget must exceed current observed spend")
            self.resolved.append((iid, body))
            return {"id": iid}
        raise AssertionError(f"unexpected {method} {path}")


class CloseLostRuns(unittest.TestCase):
    def test_sql_is_guarded(self):
        sql = heal.CLOSE_SQL
        # Only receipt-less runs that ended lost or cancelled, an hour ago or more.
        for guard in ("cost_accounting_pending", "accountingReceiptReady' = 'false'",
                      "status = 'cancelled'", "error_code = 'process_lost'", "interval '1 hour'",
                      "state = 'held'"):
            self.assertIn(guard, sql)
        self.assertNotIn("succeeded", sql)
        self.assertNotIn("delete", sql.lower())

    def test_parses_closed_runs(self):
        out = json.dumps([{"run": "r1", "agent": "a1", "status": "failed"}])
        seen = []
        closed = heal.close_lost_runs(lambda sql: seen.append(sql) or out)
        self.assertEqual(closed, [{"run": "r1", "agent": "a1", "status": "failed"}])
        self.assertEqual(seen, [heal.CLOSE_SQL])

    def test_nothing_to_close(self):
        self.assertEqual(heal.close_lost_runs(lambda sql: "[]"), [])


class ResumeIncidents(unittest.TestCase):
    def test_resumes_at_same_amount_and_leaves_overspend_paused(self):
        api = StubApi([incident("i1", "Batou", 400, 19), incident("i2", "Ishikawa", 500, 500),
                       incident("i3", "Saito", 100, 0, threshold="soft")],
                      spent={"i1": 19, "i2": 500, "i3": 0})
        resumed, refused = heal.resume_incidents(api)
        self.assertEqual(resumed, ["agent Batou"])
        self.assertEqual(len(refused), 1)
        self.assertIn("Ishikawa", refused[0])
        self.assertEqual(api.resolved[0][0], "i1")
        self.assertEqual(api.resolved[0][1]["action"], "raise_budget_and_resume")
        self.assertEqual(api.resolved[0][1]["amount"], 400)  # never raised

    def test_no_incidents(self):
        self.assertEqual(heal.resume_incidents(StubApi([], {})), ([], []))


class Redaction(unittest.TestCase):
    def test_keys_never_printed(self):
        self.assertNotIn("pcp_board_abc", heal.redact("HTTP 401 pcp_board_abc123"))


if __name__ == "__main__":
    unittest.main()
