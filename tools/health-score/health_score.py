#!/usr/bin/env python3
"""A free stand-in for CodeScene's Code Health score (1-10 per file).

Measures each source file with lizard (per function: lines, cyclomatic
complexity, arguments) plus two plain text checks, and maps them to a score:

    score = 10 - sum(weight * sqrt(excess)), clamped to 1..10

where each "excess" is how far the file goes past a CodeScene-like threshold.
The weights were fitted against real CodeScene scores (see README.md and
calibrate.py). The repo score is the LOC-weighted mean of the file scores.

    python3 health_score.py <repo-dir> [--below 9.5] [--json]
"""
import argparse
import json
import math
import os
import re
import subprocess
import sys

import lizard

# Thresholds: a function or file only costs points past these.
CCN_OVER = 8        # cyclomatic complexity per function ("Complex Method")
NLOC_OVER = 70      # lines per function ("Large Method")
ARGS_OVER = 4       # arguments per function ("Excess Function Arguments")
LOC_OVER = 500      # non-blank lines per file ("Lines of Code in a Single File")

# Points lost per sqrt(excess), fitted by calibrate.py --fit.
WEIGHTS = {
    "complex_methods": 0.34,
    "large_methods": 0.03,
    "many_args": 0.22,
    "complex_conditionals": 0.36,
    "large_file": 0.02,
}

CONDITION = re.compile(r"\b(if|while|elif|unless|when)\b|\?")
LOGICAL_OP = re.compile(r"&&|\|\||\band\b|\bor\b")
SKIP_DIRS = {".git", "node_modules", "dist", "build", "target", "vendor", "venv", ".venv"}


def measure(path, source=None):
    """Return the raw excesses for one file, or None if lizard can't read it."""
    if lizard.get_reader_for(path) is None:
        return None
    if source is None:
        with open(path, encoding="utf-8", errors="ignore") as f:
            source = f.read()
    functions = lizard.analyze_file.analyze_source_code(path, source).function_list
    lines = [line for line in source.splitlines() if line.strip()]
    return {
        "loc": len(lines),
        "complex_methods": sum(max(0, f.cyclomatic_complexity - CCN_OVER) for f in functions),
        "large_methods": sum(max(0, f.nloc - NLOC_OVER) for f in functions),
        "many_args": sum(max(0, f.parameter_count - ARGS_OVER) for f in functions),
        # A condition with two or more && / || / and / or on one line.
        "complex_conditionals": sum(
            1 for line in lines if CONDITION.search(line) and len(LOGICAL_OP.findall(line)) >= 2),
        "large_file": max(0, len(lines) - LOC_OVER),
    }


def score(m, weights=WEIGHTS):
    penalty = sum(w * math.sqrt(m[k]) for k, w in weights.items())
    return round(min(10.0, max(1.0, 10.0 - penalty)), 2)


def source_files(repo):
    try:
        out = subprocess.run(["git", "-C", repo, "ls-files"], capture_output=True,
                             text=True, check=True).stdout
        paths = out.splitlines()
    except (OSError, subprocess.CalledProcessError):  # not a git checkout
        paths = []
        for d, dirs, files in os.walk(repo):
            dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
            paths += [os.path.relpath(os.path.join(d, f), repo) for f in files]
    for rel in sorted(paths):
        if not SKIP_DIRS.intersection(rel.split("/")[:-1]) and not rel.endswith(".min.js"):
            yield rel


def scan(repo):
    results = []
    for rel in source_files(repo):
        full = os.path.join(repo, rel)
        if not os.path.isfile(full):
            continue
        m = measure(full)
        if m is None or m["loc"] == 0:
            continue
        results.append({"path": rel, "score": score(m), **m})
    return results


def repo_score(results):
    loc = sum(r["loc"] for r in results)
    return round(sum(r["score"] * r["loc"] for r in results) / loc, 2) if loc else None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("repo", help="repository or folder to scan")
    ap.add_argument("--below", type=float, metavar="SCORE", help="list only files scoring below SCORE")
    ap.add_argument("--json", action="store_true", help="print JSON")
    args = ap.parse_args(argv)

    results = scan(args.repo)
    total = repo_score(results)
    shown = [r for r in results if args.below is None or r["score"] < args.below]
    if args.json:
        print(json.dumps({"repo_score": total, "files": shown}, indent=2))
        return 0
    for r in sorted(shown, key=lambda r: (r["score"], r["path"])):
        print(f"{r['score']:5.2f}  {r['loc']:6d}  {r['path']}")
    print(f"repo score (LOC-weighted): {total}  files: {len(results)}"
          + (f"  below {args.below}: {len(shown)}" if args.below is not None else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
