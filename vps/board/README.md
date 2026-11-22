# vps/board — the waits page served at board.sectionnine.dev

`index.html` and `board-sample.json` are a copy of `hq/board/` in
ghost-in-the-machine, plus one local change (budget strip and per-agent budget/week lines, fed by
the board copy of `board.json` only; to be upstreamed there). The commit and hashes below are the
base commit and the current local hashes:

- source: https://github.com/petterfranzen/ghost-in-the-machine, `hq/board/`
- commit: 5643f3b47fb5abbd7614fdf3a7385b6cf1f98e1a (PR #18, "Build: waits board, one column per who a task waits on")
- `index.html`        sha256 `9dfadeac2e1018a1fc704ab50f8c252e37cd47a97cad67486abe55e461ab5905`
- `board-sample.json` sha256 `70031dbd84fde2c3262d3ae48ceb19006d4c84511d8340e4b5c8fa2c1e663e02`

`vps/hq-status.py status` copies both into the folder the hq container serves (`board/`) and writes
`board/board.json` beside them (the file the page polls). Nothing here is executed on the VPS: the
page is static HTML and is only served. `placeholder.html` is the previous 5 KB page, kept as the
fallback for when `index.html` is missing.

A change to this folder applies by itself once CI is green and it has soaked 30 minutes
(`vps/board/` is not in the sync's protected set; see docs/RUNBOOK.md).

To update the page: merge the change in ghost-in-the-machine, copy `hq/board/index.html` and
`hq/board/board-sample.json` here from the new commit, update the sha/sha256 above, update the inline-script hashes in the board's Content-Security-Policy (`x-hq-caddyfile` in `vps/compose.yaml`; `scripts/vps_tests/hq_sca.py` prints the expected set when they differ), and merge. `scripts/test_vps.py` re-runs the page's own data-contract checks (from
ghost-in-the-machine's `hq/board/build.sh`) so a careless edit fails CI.
