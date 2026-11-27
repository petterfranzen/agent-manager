"""Run: python3 -m unittest test_health_score (from this folder, lizard installed)."""
import os
import tempfile
import unittest

import health_score as hs

SIMPLE = '''
def add(a, b):
    return a + b
'''

MESSY = '''
def decide(a, b, c, d, e, f, g):
    if a and b or c:
        return 1
    if b and c or d:
        return 2
    for x in range(a):
        if x == b:
            return 3
        elif x == c:
            return 4
        elif x == d:
            return 5
        elif x == e:
            return 6
        elif x == f:
            return 7
        elif x == g:
            return 8
    return 0
'''


class HealthScoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        for name, text in (("simple.py", SIMPLE), ("messy.py", MESSY), ("notes.txt", "not code\n")):
            with open(os.path.join(self.dir.name, name), "w") as f:
                f.write(text)

    def tearDown(self):
        self.dir.cleanup()

    def test_simple_file_scores_ten(self):
        m = hs.measure(os.path.join(self.dir.name, "simple.py"))
        self.assertEqual(hs.score(m), 10.0)

    def test_messy_file_loses_points_for_each_smell(self):
        m = hs.measure(os.path.join(self.dir.name, "messy.py"))
        self.assertEqual(m["many_args"], 3)              # 7 arguments, 4 allowed
        self.assertEqual(m["complex_conditionals"], 2)   # two `x and y or z`
        self.assertGreater(m["complex_methods"], 0)
        self.assertLess(hs.score(m), 9.0)
        self.assertGreaterEqual(hs.score(m), 1.0)

    def test_repo_score_is_loc_weighted_and_skips_non_code(self):
        results = hs.scan(self.dir.name)
        self.assertEqual(sorted(r["path"] for r in results), ["messy.py", "simple.py"])
        by = {r["path"]: r for r in results}
        expected = (by["simple.py"]["score"] * by["simple.py"]["loc"]
                    + by["messy.py"]["score"] * by["messy.py"]["loc"]) / (
                        by["simple.py"]["loc"] + by["messy.py"]["loc"])
        self.assertAlmostEqual(hs.repo_score(results), expected, places=2)


if __name__ == "__main__":
    unittest.main()
