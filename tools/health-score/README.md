# health-score

A free stand-in for CodeScene's Code Health score: 1 to 10 per file, and a
LOC-weighted score per repo. One Python file on top of
[lizard](https://github.com/terryyin/lizard), calibrated against scores
CodeScene gave our own repos.

## Run

```bash
python3 -m pip install --require-hashes -r tools/health-score/requirements.txt
python3 tools/health-score/health_score.py <repo-dir>              # every file, worst first, then the repo score
python3 tools/health-score/health_score.py <repo-dir> --below 9.5  # only the offenders
python3 tools/health-score/health_score.py <repo-dir> --json
```

It scores the git-tracked files lizard can parse (Java, TS/JS, Python, Go,
C/C++, C#, Kotlin, Scala, Rust, Ruby and more). Shell, SQL, Clojure and
other languages lizard can't read are skipped, as CodeScene skips most of
them.

## What it measures

Per function (lizard): cyclomatic complexity, lines, arguments. Per file:
non-blank lines, and lines whose condition holds two or more `&&`/`||`/`and`/`or`.

```
score = 10 - 0.34*sqrt(complexity over 8, summed over functions)
           - 0.03*sqrt(lines over 70, summed over functions)
           - 0.22*sqrt(arguments over 4, summed over functions)
           - 0.36*sqrt(complex conditional lines)
           - 0.02*sqrt(file lines over 500)
clamped to 1..10
```

The terms follow CodeScene's biomarkers (Complex Method, Large Method,
Excess Function Arguments, Complex Conditional, Lines of Code in a Single
File); the thresholds came from a grid search, which settled on values
close to CodeScene's defaults. The square root makes
the first problem cost the most, as CodeScene's scores do. The JSON output
lists each excess, so you can see why a file lost points.

## Calibration

`calibration/*.csv` holds CodeScene's own per-file scores for our six public
repos (flight-tracker, ghost-in-the-machine, portfolio, dinner-planner,
nvim-trainer, agent-manager) on 16 Nov 2026 (older code, many low scores)
and 27 Nov 2026, with the commit each was taken at. A file unchanged between
the two counts once: 742 distinct files, 118 of them below 9.5.

`calibrate.py` reads each file at its commit from local clones, fits the
five weights by non-negative least squares, and prints the numbers below:

```bash
python3 tools/health-score/calibrate.py <dir-with-clones-of-the-six-repos> [--fit]
```

Leave-one-repo-out (the weights fitted on five repos, scored on the sixth),
which is the honest measure; in-sample numbers are within 0.01-0.02:

| group   | files | CodeScene < 9.5 | Spearman | MAE  | "< 9.5" precision | recall |
|---------|------:|----------------:|---------:|-----:|------------------:|-------:|
| Java    | 290   | 43              | 0.73     | 0.13 | 0.79              | 0.63   |
| TS/JS   | 362   | 55              | 0.79     | 0.14 | 0.67              | 0.69   |
| Python  | 44    | 8               | 0.75     | 0.25 | 0.73              | 1.00   |
| other   | 46    | 12              | 0.69     | 0.27 | 0.77              | 0.83   |
| overall | 742   | 118             | 0.75     | 0.15 | 0.72              | 0.70   |

For scale: calling every file 10.0 gives an MAE of 0.32. Most files score
exactly 10 in both tools, and those ties hold the Spearman figure down.

LOC-weighted repo score over the same files, CodeScene vs this tool:

| repo                 | 16 Nov      | 27 Nov      |
|----------------------|-------------|-------------|
| agent-manager        | 5.30 / 5.37 | 9.99 / 9.76 |
| dinner-planner       | 9.62 / 9.78 | 9.93 / 9.99 |
| flight-tracker       | 9.11 / 9.25 | 9.81 / 9.76 |
| ghost-in-the-machine | 7.16 / 7.47 | 8.65 / 8.79 |
| nvim-trainer         | 9.07 / 9.20 | 9.88 / 9.89 |
| portfolio            | 8.77 / 9.17 | 9.81 / 9.68 |

## Limits

- It misses what it can't see: CodeScene's duplication (similar functions,
  duplicated assertion blocks in tests), low cohesion, Bumpy Road, primitive
  obsession. Its worst misses are tests that CodeScene marks down for
  repeated assertion blocks (several flight-tracker Java tests at 8.0-8.5
  score 10 here). In-file duplicate detection (exact and identifier-blind
  line windows) and an assertion-block count were tried and didn't improve
  the fit, so they're left out.
- About 3 in 10 files it flags below 9.5 are 9.5+ in CodeScene, and it
  misses about 3 in 10 that CodeScene flags. Treat a single file's score as
  a pointer to look, not a verdict; the repo score and its trend are the
  reliable part (within 0.4 of CodeScene on every repo above).
- The complex-conditional check is a per-line regex, so a long condition
  split over lines escapes it and `?` in a string can trip it.
- Calibrated on six repos by one team; other code styles may fit worse.
  Refit with `calibrate.py --fit` when new CodeScene scores are available.
