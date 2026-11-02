# agent-manager

Section 9: self-hosted AI agents on a VPS, with Claude (Claude Code, on Petter's
plan) as coordinator. The Hetzner VM keeps only flight-tracker and this repo's
sync timer.

- **Tasks** are issues in the private repo `section9-hq` (labels `agent:<name>`,
  `status:*`, `waits:*`). `vps/dispatch.py` runs one `status:todo` task at a time.
- **Hermes Agent** does the work, on cheap models through OpenRouter (zero data
  retention, no xAI).
- A **sandbox** container is where agents run commands; it never holds model keys.

Start with [docs/DECISIONS.md](docs/DECISIONS.md) for what was decided and why, then
[docs/RUNBOOK.md](docs/RUNBOOK.md) for the VPS. [docs/DESIGN.md](docs/DESIGN.md) is the
original design, partly history.
