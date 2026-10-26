# agent-manager

Self-hosted AI agents on the Hetzner VM that also runs flight-tracker, with
Claude (Claude Code, on Petter's plan) as coordinator.

- **Paperclip** is the manager: org chart, tasks, budgets, approvals, routines, dashboard.
- **Hermes Agent** does the work, on cheap models through OpenRouter (zero data retention, no xAI).
- A **sandbox** container is where agents run commands; it never holds model keys.

Start with [docs/DESIGN.md](docs/DESIGN.md), then [docs/RUNBOOK.md](docs/RUNBOOK.md) to deploy.
