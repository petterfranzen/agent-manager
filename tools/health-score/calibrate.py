#!/usr/bin/env python3
"""Check (and optionally refit) health_score.py against saved CodeScene scores.

    python3 calibrate.py <clones-dir> [--fit]

<clones-dir> holds a git clone of each repo named in calibration/*.csv
(e.g. <clones-dir>/flight-tracker). Each file is read at the CSV's commit
with `git show`, so no checkout is needed. Prints Spearman, mean absolute
error and the "below 9.5" precision/recall per language and overall, both
for the weights in health_score.py and leave-one-repo-out (weights refitted
without the repo being scored), then the LOC-weighted repo scores.
--fit prints the weights a non-negative least-squares fit gives.
"""
import argparse
import csv
import glob
import hashlib
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import health_score as hs  # noqa: E402

LANG = {".java": "Java", ".py": "Python", ".ts": "TS/JS", ".tsx": "TS/JS",
        ".js": "TS/JS", ".mjs": "TS/JS", ".cjs": "TS/JS"}
KEYS = list(hs.WEIGHTS)


def load(clones):
    rows = []
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), "calibration", "*.csv"))):
        with open(path, newline="") as f:
            for r in csv.DictReader(f):
                src = subprocess.run(["git", "-C", os.path.join(clones, r["repo"]), "show",
                                      f"{r['commit']}:{r['path']}"],
                                     capture_output=True, text=True, errors="ignore", check=True).stdout
                m = hs.measure(r["path"], src)
                if m is None:
                    continue
                rows.append({**r, "snapshot": os.path.basename(path)[-14:-4], "m": m,
                             "cs": float(r["codescene_score"]), "loc": int(r["loc"]),
                             "key": (hashlib.sha1(src.encode()).hexdigest(), r["codescene_score"]),
                             "lang": LANG.get(os.path.splitext(r["path"])[1], "other")})
    return rows


def nnls(rows):
    """Weights >= 0 minimising sum((10 - cs - X.w)^2), by coordinate descent."""
    X = [[hs.math.sqrt(r["m"][k]) for k in KEYS] for r in rows]
    y = [10 - r["cs"] for r in rows]
    w = [0.0] * len(KEYS)
    resid = y[:]
    for _ in range(500):
        for j in range(len(KEYS)):
            col = [x[j] for x in X]
            norm = sum(c * c for c in col)
            if not norm:
                continue
            new = max(0.0, w[j] + sum(c * e for c, e in zip(col, resid)) / norm)
            resid = [e - (new - w[j]) * c for e, c in zip(resid, col)]
            w[j] = new
    return dict(zip(KEYS, w))


def ranks(v):
    order = sorted(range(len(v)), key=v.__getitem__)
    out = [0.0] * len(v)
    i = 0
    while i < len(v):
        j = i
        while j + 1 < len(v) and v[order[j + 1]] == v[order[i]]:
            j += 1
        for k in order[i:j + 1]:
            out[k] = (i + j) / 2
        i = j + 1
    return out


def spearman(a, b):
    ra, rb = ranks(a), ranks(b)
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = (sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb)) ** 0.5
    return cov / den if den else float("nan")


def report(label, rows, pred):
    print(f"\n{label}")
    print(f"  {'group':8} {'files':>5} {'<9.5':>5} {'rho':>6} {'MAE':>6} {'prec':>5} {'recall':>6}")
    for group in sorted({r["lang"] for r in rows}) + ["overall"]:
        idx = [i for i, r in enumerate(rows) if group in ("overall", r["lang"])]
        p, y = [pred[i] for i in idx], [rows[i]["cs"] for i in idx]
        tp = sum(a < 9.5 and b < 9.5 for a, b in zip(p, y))
        flagged, actual = sum(a < 9.5 for a in p), sum(b < 9.5 for b in y)
        print(f"  {group:8} {len(idx):5d} {actual:5d} {spearman(p, y):6.2f} "
              f"{sum(abs(a - b) for a, b in zip(p, y)) / len(idx):6.2f} "
              f"{tp / flagged if flagged else float('nan'):5.2f} {tp / actual if actual else float('nan'):6.2f}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("clones")
    ap.add_argument("--fit", action="store_true")
    args = ap.parse_args()
    every = load(args.clones)
    seen, rows = set(), []
    for r in every:  # a file unchanged between snapshots counts once
        if r["key"] not in seen:
            seen.add(r["key"])
            rows.append(r)
    print(f"{len(rows)} distinct scored files ({len(every)} rows)")
    if args.fit:
        print("fitted weights:", {k: round(v, 3) for k, v in nnls(rows).items()})

    report("weights in health_score.py", rows, [hs.score(r["m"]) for r in rows])
    loro = [0.0] * len(rows)
    for repo in {r["repo"] for r in rows}:
        w = nnls([r for r in rows if r["repo"] != repo])
        for i, r in enumerate(rows):
            if r["repo"] == repo:
                loro[i] = hs.score(r["m"], w)
    report("leave-one-repo-out (weights fitted without the repo scored)", rows, loro)

    print("\nLOC-weighted repo score: CodeScene vs this tool")
    for key in sorted({(r["repo"], r["snapshot"]) for r in every}):
        rs = [r for r in every if (r["repo"], r["snapshot"]) == key]
        loc = sum(r["loc"] for r in rs)
        cs = sum(r["cs"] * r["loc"] for r in rs) / loc
        ours = sum(hs.score(r["m"]) * r["loc"] for r in rs) / loc
        print(f"  {key[0]:22} {key[1]}  {cs:5.2f}  {ours:5.2f}  ({len(rs)} files)")


if __name__ == "__main__":
    main()
