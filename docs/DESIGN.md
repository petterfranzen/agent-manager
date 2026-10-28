# Design

## What this is for

Petter runs a small fleet of AI agents on the same Hetzner VM as
flight-tracker. Claude, in Claude Code sessions on Petter's plan, coordinates
them: it writes briefs, reviews work, merges, and reports. Cheap models
on OpenRouter do the bulk of the work:

- **Tachikoma, nightly builder:** each night, picks a language and an idea it hasn't done
  before, builds a small app in `ghost-in-the-machine`, and leaves a preview
  link for the morning.
- **Ishikawa, researcher:** research tasks and reports.
- **Batou, coder:** small fixes and first drafts in `ghost-in-the-machine`.
- **Saito, ops:** read-only watch over the VM; reports to the coordinator.
- **Togusa, backup coordinator:** keeps work moving when Claude is unavailable.

Names come from Section 9 in *Ghost in the Shell* (Claude is the Major, Petter
the Chief); see [agents/README.md](../agents/README.md) for avatars and personalities.

## Architecture

```mermaid
flowchart TD
  P[Petter] -->|Claude app| C[Claude: coordinator<br/>check-ins 07:47 · 11:47 · 18:47]
  C -->|Paperclip API via Cloudflare Access| PC[Paperclip<br/>org chart · tasks · budgets · routines]
  P -->|browser via Cloudflare Access| PC
  PC -->|hermes_gateway, localhost| H[Hermes gateway<br/>holds the OpenRouter key]
  H -->|model calls| OR[OpenRouter<br/>ZDR only, no xAI, $20/month key]
  H -->|SSH: commands, no provider keys| S[Sandbox container<br/>git, toolchains, workspace]
  S -->|branches + PRs| G[GitHub: ghost-in-the-machine]
  G -->|Pages| W[Morning preview]
  C -->|review + merge| G
```

| Container | Image | Memory cap | Notes |
|---|---|---|---|
| `postgres` | `postgres:17-alpine` (digest pinned) | 256 MB | Paperclip's database |
| `paperclip` | `ghcr.io/paperclipai/paperclip:2026.1023.0` (digest pinned) | 768 MB | UI and API on `127.0.0.1:3100`; cloudflared publishes it as `hq.sectionnine.dev` |
| `hermes` | `nousresearch/hermes-agent:v0.22.5` (digest pinned) | 512 MB | Gateway API on port 8642. It shares `paperclip`'s network namespace, so Paperclip reaches it on loopback, the only plain-HTTP target Paperclip allows. |
| `sandbox` | built from `sandbox/Dockerfile` | 1.5 GB, 1.5 CPU | Where agent commands run, reached over SSH. Holds the GitHub token, never the OpenRouter key. |

**Memory.** The VM has 3.8 GB of RAM and 4 GB of swap. Flight-tracker uses about
0.6–0.8 GB after #132, and the OS plus cloudflared about 0.4 GB. The caps add
up to 3 GB, but they are ceilings, not usage: idle, the stack should sit around
1–1.5 GB. Builds that spike use swap. `vm.swappiness=10` keeps flight-tracker in
RAM. Every container is capped, so a runaway build is OOM-killed inside its
container and can't take flight-tracker with it. The coordinator watches
`/api/ops/stats` and flags pressure.

## Deploys

The VM pulls; nothing pushes to it. This keeps the Major off SSH and out of
the VM's secrets.

1. A PR merges to `main` and CI runs on the merge commit.
2. `agent-manager-sync.timer` starts `agent-manager-sync` every 5 minutes. It
   fetches `main` and exits unless there is a commit it hasn't applied.
3. It reads the commit's check runs from GitHub's public API and waits until the
   required ones all succeeded. It refuses a dirty working tree, a `main` that
   was force-pushed, and a commit that has already failed three times.
4. It fast-forwards `/opt/agent-manager`, runs `docker compose up -d --build`,
   restarts Hermes if Paperclip's container was replaced, waits for Paperclip's
   health check and runs `scripts/paperclip-apply.py`.
5. It records the result in `/var/lib/agent-manager/sync-state.json` and posts a
   comment on the `Deploys` issue in Paperclip: commit, result, duration and the
   roster changes (or the end of the error). There is no automatic rollback.

Anyone who can merge to `main` can run code as root on the VM, so `main` is
protected by a ruleset (PR required, green CI). Setup is in
[RUNBOOK.md](RUNBOOK.md), step 10.

## Models (OpenRouter, ZDR endpoints only)

| Use | Model | $/M tokens in / out |
|---|---|---|
| Default for every agent | `deepseek/deepseek-v4.1-flash` | 0.05 / 0.19 |
| Fallback 1, backup coordinator | `z-ai/glm-5.3-flash` | 0.15 / 0.50 |
| Fallback 2 | `minimax/minimax-m3` | 0.30 / 1.20 |
| Fallback 3 (Claude via Bedrock/Vertex) | `anthropic/claude-haiku-5.5` | 0.10 / 0.50 |

These were chosen from OpenRouter's ZDR endpoint list on 2026-10-26.
Harness-Bench (arXiv 2605.27922) scored Hermes with DeepSeek-V4-Flash and
GLM-5.3-Flash at about 67, level with frontier models in other harnesses. The
best harness depends on the model, so the coder agent may move to OpenCode
later. Paperclip supports it, and the switch is per agent.

Account settings, done 2026-10-26:
- ZDR enforced for all providers. Google is "safety retention allowed", so Gemini stays available through Vertex.
- No training, no data sharing.
- The key has a $20 monthly budget.

**No xAI models.** That's Petter's choice: SpaceXAI ZDR is on, `provider_routing.ignore`
lists `x-ai`, and no config names an `x-ai/*` model.

## Images

`paperclip` runs a slim repackaging of the official Paperclip image, not the
official one. The official image is 3.4 GB compressed. About two thirds of that
is the global agent CLIs (claude-code, codex, opencode, gemini-cli, kimi-code)
and a provider pack for remote OpenCode/ACPX runs. Our agents use the
`hermes_gateway` adapter, so Paperclip never starts any of them, and the 83%
full VM disk can't spare the room. With them gone, nothing next to
`DATABASE_URL` can run an agent CLI.

`images/paperclip/Dockerfile` copies the application from the official image
(pinned by digest) onto the same Node base, with upstream's apt packages, user
setup, entrypoint, environment and default command. Only the CLIs and the
provider pack are left out. `.github/workflows/paperclip-image.yml` builds it
on every change, starts it against Postgres, checks that its config matches
upstream's and that the CLIs are gone, and on `main` pushes it to
`ghcr.io/petterfranzen/paperclip-slim`. Once it is published, `compose.yaml`
pins it by digest. To bump Paperclip, follow the comment at the top of the Dockerfile.

## Rules

- **Server agents never merge.** They push branches and open PRs, and the
  coordinator reviews and merges. `ghost-in-the-machine`'s `main` is protected
  by a ruleset with no bypass, so a push straight to `main` fails even with
  Petter's token.
- **Agents can reach one repo only.** A fine-grained token covers `ghost-in-the-machine`
  alone, with contents, PRs and issues read/write and Actions and statuses
  read. It has no Workflows or Administration rights, so agents can't change
  CI or settings. Flight-tracker stays with the coordinator, because a push to
  its `main` deploys to production.
- **Secrets never land in git.** The sandbox has no model keys. GitHub push
  protection is on, and a pre-push hook and CI run gitleaks. Every repo is
  public.
- **Patch rule (Petter's):** a major CVE (CVSS 7+ or CISA KEV) in anything
  running gets patched no sooner than 24 h and no later than 72 h after the fix
  is released. Images are pinned by digest, and updates come in through
  Dependabot PRs, merged only once the release is 24 h old.
- **Budgets:** the OpenRouter key stops at $20 a month. Paperclip budgets are set
  per agent (see `agents/README.md`), so one agent can't use up the month.
- **Ops is read-only.** It reports; changes go through the coordinator and Petter.

## Nightly builder routine

At 01:00 Stockholm, a Paperclip routine creates a task for the nightly builder:

1. Read `ghost-in-the-machine/LOG.md`, the history of past nights (language,
   idea, verdict, Petter's feedback), and pick a language and app idea not done
   before. Relaxing, enjoyable apps are the default theme.
2. Sparse-clone: `git clone --filter=blob:none --sparse`, then
   `git sparse-checkout set <night-folder>`.
3. Build in `nights/YYYY-MM-DD-<slug>/` with a README (idea, language, how
   to run), a build script, and a static web build if the language allows it
   (WebAssembly, JS output), so it can be previewed on Pages.
4. Open a PR with a summary and screenshots. The coordinator's morning
   check-in reviews it, merges if it's sound, and sends Petter the preview link.
5. Append the night to `LOG.md` and stop. If it runs out of budget, it stops
   and says why.

Petter's feedback goes into `LOG.md` and shapes the following nights.

## Coordinator interface

Claude reaches Paperclip at `https://hq.sectionnine.dev` through Cloudflare
Access, using the same `claude-ops-stats` service token as the flight-tracker
stats endpoint. Paperclip's board API key is stored in the Claude Code
environment's secrets as `PAPERCLIP_API_KEY`. The check-ins read open tasks,
results and budgets, and the coordinator writes tasks and comments. Rules for
Claude are in [`../CLAUDE.md`](../CLAUDE.md).

## Domains

`sectionnine.dev` is the Hetzner VM's domain (Section 9: the agents and, after
its move, flight-tracker). `pastabake.cloud` is the homelab's.

## Open items

- A 24 h delay on unattended-upgrades for cloudflared (patch rule).
- OpenCode as the coder's runtime, after a week of data.
- SSH from the CachyOS desktop (the laptop attempt failed).
