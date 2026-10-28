# Agents

Each file here is a Paperclip agent's instructions. `roster.toml` lists the
agents, their budgets, avatars and routines; `scripts/paperclip-apply.py`
applies it to Paperclip with the **Hermes gateway** adapter, sending each brief
as the agent's instructions (see docs/RUNBOOK.md, step 7).

The team is named after Section 9 in *Ghost in the Shell*. Petter is **the
Chief**, Claude (the coordinator) is **the Major** (avatar: `avatars/major.png`; the Major isn't a Paperclip agent), and the agents are:

| | Name | Role | Brief | Reports to | Monthly budget | Wakes on |
|---|---|---|---|---|---|---|
| <img src="avatars/tachikoma.png" width="48"> | **Tachikoma** | Nightly builder | [nightly-builder.md](nightly-builder.md) | The Major | $10 | Routine, 01:00 Stockholm |
| <img src="avatars/batou.png" width="48"> | **Batou** | Coder | [coder.md](coder.md) | The Major | $4 | Assigned tasks |
| <img src="avatars/ishikawa.png" width="48"> | **Ishikawa** | Researcher | [researcher.md](researcher.md) | The Major | $3 | Assigned tasks |
| <img src="avatars/togusa.png" width="48"> | **Togusa** | Backup coordinator | [backup-coordinator.md](backup-coordinator.md) | The Chief | $2 | Routine, every 6 h, acts only if the Major is silent |
| <img src="avatars/saito.png" width="48"> | **Saito** | Ops (read-only) | [ops.md](ops.md) | The Major | $1 | Routine, 06:30 Stockholm |

**Copying Tachikoma:**
- Hire another agent with the same brief and adapter settings, named
  `Tachikoma 2`, `Tachikoma 3` and so on, reporting to the Major.
- Give each copy its own routine, staggered (for example 01:00, 01:20 and 01:40
  Stockholm), and its own budget. The copies share the nightly builder's $10
  unless Petter raises the OpenRouter key's $20 cap. Each night costs roughly
  $0.30.
- Copies are safe in parallel: separate run folders, separate branches, and the
  Major merges.

Each brief opens with a short personality. It sets the agent's voice in
comments and PRs, never its quality bar or rules.

**Avatars** are original drawings in `avatars/` (SVG sources made by
`make.py`, rendered to 512 px PNGs by `render.js`). The roster
script uploads them through Paperclip's API (runbook step 7).

The budgets add up to $20, the OpenRouter key's monthly cap. Paperclip stops
an agent at its budget, and the key stops everything at $20.

**Rules every agent follows (repeated in each brief):**
- Never merge. Open a PR and stop. The coordinator merges.
- Never commit secrets. The repo is public. The pre-push hook and push
  protection will block it, but don't rely on them.
- Work only in `petterfranzen/ghost-in-the-machine`. You have no access to
  anything else.
- Stop and report when blocked, out of budget or unsure. Don't guess around it.
