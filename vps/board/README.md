# vps/board — the waits page served at hq.sectionnine.dev/board

`index.html` and `board-sample.json` are a byte-for-byte copy of `hq/board/` in
ghost-in-the-machine:

- source: https://github.com/petterfranzen/ghost-in-the-machine, `hq/board/`
- commit: 5643f3b47fb5abbd7614fdf3a7385b6cf1f98e1a (PR #18, "Build: waits board, one column per who a task waits on")
- `index.html`        sha256 `b0682c5ea406c71b78c42a467a1840d925612a0b138d7b0562d9546303152c8e`
- `board-sample.json` sha256 `3595bd11af76c03953e05e1ea79150a3134908ef49cb0220e036a7ebb0343fe3`

`vps/hq-status.py status` copies both into the folder the hq container serves (`board/`) and writes
`board/board.json` beside them (the file the page polls). Nothing here is executed on the VPS: the
page is static HTML and is only served. `placeholder.html` is the previous 5 KB page, kept as the
fallback for when `index.html` is missing.

A change to this folder applies by itself once CI is green and it has soaked 30 minutes
(`vps/board/` is not in the sync's protected set; see docs/RUNBOOK.md).

To update the page: merge the change in ghost-in-the-machine, copy `hq/board/index.html` and
`hq/board/board-sample.json` here from the new commit, update the sha/sha256 above, and merge. `scripts/test_vps.py` re-runs the page's own data-contract checks (from
ghost-in-the-machine's `hq/board/build.sh`) so a careless edit fails CI.
