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
| 2026-10-28 | Tachikoma's nightly routine is on (01:00 Stockholm). Other routines stay paused until each agent's supervised first run. | His first night shipped after one review round, and Petter liked it. |
| 2026-10-30 | True OpenRouter cost and remaining budget live in the Paperclip "Spend" issue's description, rewritten hourly by the VM; the Major and the check-in read it from there. No OpenRouter key goes to cloud sessions. | Petter wants real money in the scheduled updates and on request. The key stays on the VM; a normal key only sees itself and a management key can mint keys. |
| 2026-10-30 | Spend is booked in Paperclip every hour, not daily; the company budget is $19, under the OpenRouter key's $20 hard cap; unpriced usage stays "allow". | Petter: cost and budget must be present throughout, so Paperclip neither stops agents for lack of data nor lets them overspend. Hourly booking keeps budgets within an hour of reality; $19 makes Paperclip pause the agents before the key runs dry mid-run; "block" would pause on every Hermes run, which is always unpriced. |
| 2026-10-30 | Real spend comes from OpenRouter: a daily VM job (`agent-manager-spend.timer`, 07:20 Stockholm) reads the key's usage, books each day's spend in Paperclip as cost events split by the agents' token share, and posts it on the "Spend" issue. Tachikoma aims for 60–120 minutes a night. | Hermes reports tokens but no price, so Paperclip showed $0. The real figure was $1.12 for 27–30 Oct (89% cache hits, ~$0.01 per million tokens), so the $20 cap leaves room for longer nights. |
| 2026-10-29 | Research goes to Section 9 (Ishikawa first), now or overnight. The Major asks Petter only when something is urgent. | Petter's call: keep him out of the loop for non-urgent work. |
| 2026-10-29 | flight-tracker gets `MemoryLow=1100M` (Petter runs `systemctl set-property` once; the repo's unit file follows with the next flight-tracker change). The check-in flags harm (refaults, read rate, PSI), not the soft-limit counter. The Deploys comment shows VM memory. | On 28–29 Oct, agent builds on the shared 3.8 GB VM made the kernel evict flight-tracker's database cache (743 → 300 MB), and 4.7 GB was re-read overnight. App memory stayed flat (~610 MB), so there's no leak. |
| 2026-10-28 | Each agent gets a Paperclip `task_bridge` key, used through `pc-task` in the sandbox, to set the status of and comment on its own issues (Petter: "we need a solution"). `paperclip-apply.py` mints them (boundary: project "Agent work") into `/etc/agent-manager/bridge-keys/`, mounted read-only into the sandbox. | Without it every run ended with the issue unresolved, Paperclip marked it blocked and spent recovery runs on it, and a timed-out issue could not be restarted. A `task_bridge` key reaches only issues assigned to its agent: no company-wide reads, other agents, secrets or wakes. Accepted risk: all five keys sit in the one shared sandbox, so code run there could use another agent's key on that agent's issues. The keys work only inside the VM; Cloudflare Access fronts the public URL. |
| 2026-10-28 | The sync removes every Docker image no container uses, with no age filter. | `--filter until=168h` goes by build date, so the replaced 8.9 GB Paperclip image (built 23 Oct) stayed and the disk hit 95%. Rollbacks re-pull by digest. |
| 2026-10-28 | Run timeouts: Batou and Ishikawa 60 minutes (`timeout_sec = 3600`), Tachikoma 3 hours (was 2) so he can plan and explore ideas before building. | Batou's and Ishikawa's first tasks, the crew page redesign and the Stockholm housing research, both hit the 10-minute default and ended in error with nothing delivered. The $20 company cap bounds the cost of longer runs (Petter). |
| 2026-10-28 | Run a slim repackaging of the official Paperclip image, built in GitHub Actions to ghcr.io/petterfranzen/paperclip-slim and pinned by digest: no agent CLIs, no provider pack. | The official image is 3.4 GB compressed, two-thirds of it unused by our Hermes setup; the VM disk was 83% full. Fewer binaries next to the database credentials. |
| 2026-10-28 | The VM pulls agent-manager's main every 5 minutes and applies a commit only when all its CI checks passed; results are posted to a "Deploys" issue in Paperclip. main is protected by a ruleset. | Petter wants the Major to push config without him running commands, without giving the Major SSH or new keys. |
| 2026-10-28 | Paperclip budgets allow unpriced usage; the OpenRouter key's $20 cap is the real limit. Sign-up closed, announcements off. | Hermes reports tokens, not cost, so the default "block" paused the whole company after Tachikoma's first run. |
| 2026-10-27 | Weekday mornings, the check-in's push ends with one gentle backlog question (skipped if Petter added something in the last day). No extra notifications. | Petter wants a backlog of ideas and tasks to build up without being nagged. |
| 2026-10-27 | Paperclip's agents, budgets, routines and avatars become a roster file applied by a script, not UI clicks. | Recreatable from code. |
| 2026-10-27 | No off-host backups of Paperclip's database or Hermes's memory. Paperclip's own hourly on-host backups stay on (restore steps in `docs/paperclip.md`); they help after a bad upgrade. | Agent history isn't valuable enough; everything else is in GitHub. |
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
- Paperclip plugins: none for now. The Cloudflare sandbox plugin doesn't support the Hermes adapter, and plugin code runs unsandboxed in the Paperclip container. Revisit when that changes (`docs/paperclip-plugins.md`).
- Give the sandbox a headless browser, so the agents can check what they build renders (Tachikoma's first night missed two visible bugs).
- Apply the roster on the VM and switch Saito to the Hermes gateway.
- Move flight-tracker to `flight.sectionnine.dev` and an SSH hostname on the same domain.
- flight-tracker freshness: separate sweep backoff, OpenSky rate-limit headers, honest `lastSweepAt`, OpenSky OAuth.
- 24 h delay for cloudflared in unattended-upgrades (patch rule).
- Generic error bodies in flight-tracker, so the framework can't be fingerprinted.
