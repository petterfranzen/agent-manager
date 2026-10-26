# Agents

Each file here is a Paperclip agent's instructions. Hire each one in Paperclip
with the **Hermes gateway** adapter (see docs/RUNBOOK.md, step 7) and paste its
brief as the agent's instructions.

| Agent | Brief | Reports to | Monthly budget | Wakes on |
|---|---|---|---|---|
| Nightly builder | [nightly-builder.md](nightly-builder.md) | Coordinator (Claude) | $10 | Routine, 01:00 Stockholm |
| Coder | [coder.md](coder.md) | Coordinator | $4 | Assigned tasks |
| Researcher | [researcher.md](researcher.md) | Coordinator | $3 | Assigned tasks |
| Backup coordinator | [backup-coordinator.md](backup-coordinator.md) | Petter | $2 | Routine, every 6 h, acts only if Claude is silent |
| Ops | [ops.md](ops.md) | Coordinator | $1 | Routine, 06:30 Stockholm |

The budgets add up to $20, the OpenRouter key's monthly cap. Paperclip stops
an agent at its budget, and the key stops everything at $20.

**Rules every agent follows (repeated in each brief):**
- Never merge. Open a PR and stop. The coordinator merges.
- Never commit secrets. The repo is public. The pre-push hook and push
  protection will block it, but don't rely on them.
- Work only in `petterfranzen/ghost-in-the-machine`. You have no access to
  anything else.
- Stop and report when blocked, out of budget or unsure. Don't guess around it.
