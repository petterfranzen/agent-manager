# agent-manager

Self-hosted AI agents (moving from the Hetzner VM, which keeps only flight-tracker, to a VPS: see docs/RUNBOOK.md), with
Claude (Claude Code, on Petter's plan) as coordinator.

- **Paperclip** is the manager: org chart, tasks, budgets, approvals, routines, dashboard.
- **Hermes Agent** does the work, on cheap models through OpenRouter (zero data retention, no xAI).
- A **sandbox** container is where agents run commands; it never holds model keys.

Start with [docs/DESIGN.md](docs/DESIGN.md), then [docs/RUNBOOK.md](docs/RUNBOOK.md) to deploy.
