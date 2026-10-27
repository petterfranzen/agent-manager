# Decisions

The Major's memory: what Petter and Claude decided, and why. Newest first.
Read this at the start of a session; add a line whenever something is decided.
Personal notes and reminders live in Petter's Daybook, never here (this repo is
public).

## Principles
- **Recreatable from code.** The whole environment lives in GitHub (and the
  Daybook artifact for personal things). Secrets live in Petter's password
  manager and go back onto a VM with `scripts/new-secrets.sh`. Nothing on the
  VM should be irreplaceable, so there are no backups of agent history.
- **Agents are ephemeral.** They're defined by their briefs in `agents/`; any
  of them can be removed and rehired. Their run history isn't kept on purpose.
- **Petter's patch rule.** A release is installed once it's 24 h old; a major
  CVE fix (CVSS 7+ or CISA KEV) within 72 h. Images and tools are pinned.

## Log

| Date | Decision | Why |
|---|---|---|
| 2026-10-27 | Paperclip's agents, budgets, routines and avatars become a roster file applied by a script, not UI clicks. | Recreatable from code. |
| 2026-10-27 | No backups of Paperclip's database or Hermes's memory. | Agent history isn't valuable enough; everything else is in GitHub. |
| 2026-10-27 | `sectionnine.dev` (Cloudflare Registrar) is the Hetzner VM's domain; `pastabake.cloud` stays with the homelab. Paperclip is `hq.sectionnine.dev`; flight-tracker moves later. | Separate the homelab from Section 9. |
| 2026-10-27 | `pastabake.cloud` (Porkbun, expires 2027-02-16): transfer to Cloudflare or replace for the NAS, decided by mid-January. | Manage domains in one place. |
| 2026-10-27 | Paperclip runs from environment variables only; the first admin is claimed with the CLI's bootstrap invite. | Browser claim is disabled in authenticated + public mode. |
| 2026-10-27 | Hermes's dashboard and webhook listeners stay off. | Nothing in the stack listens beyond loopback except Paperclip on `127.0.0.1:3100`. |
| 2026-10-27 | Tachikoma can be copied; copies claim the night's language with an early draft PR. | Several nightly builds in parallel without collisions. |
| 2026-10-27 | The agents are named after Section 9 (*Ghost in the Shell*): Tachikoma, Batou, Ishikawa, Saito, Togusa. Claude is the Major, Petter the Chief. Avatars are original drawings. | Petter's choice; originals because the repo is public. |
| 2026-10-27 | Claude hands small, mechanical jobs to Sonnet sub-agents, each in its own git worktree. | Petter's preference; parallel sub-agents in one checkout collide. |
| 2026-10-27 | Claude Desktop on Petter's CachyOS desktop, from the AUR package `claude-desktop` (Anthropic's `.deb` repackaged). No Claude SSH sessions on the VM. | Hands-on work locally; the production VM changes only through PRs or commands Petter runs. |
| 2026-10-27 | SSH to the VM from CachyOS through `cloudflared access ssh`; keys imported from GitHub with `ssh-import-id-gh`. | The Hetzner web console garbles pastes. |
| 2026-10-26 | Paperclip (manager) + Hermes Agent (runtime) on the same Hetzner VM, in containers. Agent commands run in a separate sandbox container over SSH. | Paperclip gives the org chart, budgets and routines; Hermes runs most models. nanobot was considered and rejected (last and most expensive on Harness-Bench). |
| 2026-10-26 | Models through OpenRouter only: ZDR endpoints, no training, no xAI, a $20/month key. | Cheap execution, Claude for planning and review; xAI excluded on principle. |
| 2026-10-26 | Claude is the coordinator, reached through the Claude app; no Telegram or other IM. | One place to talk; check-ins three times a day push to the phone. |
| 2026-10-26 | Agents work only in `ghost-in-the-machine` (public, GitHub Pages) and never merge. Claude merges green PRs after review, flight-tracker production included, and sends Petter a summary after each flight-tracker deploy. | Mostly autonomous, with one reviewer in front of production. |
| 2026-10-26 | Personal to-dos, notes and reminders live in the Daybook artifact and never go to agents, OpenRouter or the VM. | Privacy. |
| 2026-10-26 | flight-tracker: keep data fresh headless; load the UI on request; update the active view first and the rest eventually. No availability target. | It's a personal app, shown to people now and then. |
| 2026-10-26 | flight-tracker stays on Spring Boot (4.1.2, 384 MB heap, SQLite cache 8 MB); Rust rewrite rejected, native image deferred. | The idle disk reads came from cgroup page-cache pressure, which #132 fixed; Rust would save memory, not I/O, at weeks of cost. |
| 2026-10-26 | `/api/ops/stats` behind Cloudflare Access with JWT verification; field names reveal no technology. | Monitoring without fingerprinting the stack. |

## Open
- Roster-as-code script for Paperclip (in progress).
- Pin `HERMES_DASHBOARD=0` and fail CI if a webhook listener is enabled.
- Move flight-tracker to `flight.sectionnine.dev` and an SSH hostname on the same domain.
- flight-tracker freshness: separate sweep backoff, OpenSky rate-limit headers, honest `lastSweepAt`, OpenSky OAuth.
- 24 h delay for cloudflared in unattended-upgrades (patch rule).
- Generic error bodies in flight-tracker, so the framework can't be fingerprinted.
