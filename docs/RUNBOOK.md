# Runbook: deploy the agent stack on the VM

> **2 Nov 2026:** the Paperclip stack is gone from this repo (root `compose.yaml`, `scripts/paperclip-apply.py`, `deploy/` accounting scripts, `docs/paperclip.md`, `scripts/new-secrets.sh`). Sections below that mention them describe the retired Hetzner setup and are history; the VPS sections are current.

## Section 9 on the VPS: Monday 2 Nov (do these in order)

Paperclip is gone. On the VPS, Hermes and the sandbox run from `vps/compose.yaml`;
`dispatch.py` (systemd timers, every 30 s) feeds them GitHub issues from the
private repo `section9-hq`. Plan and reasons: [migration-vps.md](migration-vps.md).
A run that hits its per-agent timeout (`task #172`) is left `status:blocked` with the
partial output it had written as a comment, and the dispatcher never logs a token;
set the task back to `status:todo` to run it again.
You need: root SSH to the VPS (`ssh vps` below) and to the Hetzner VM (`ssh hetzner`),
and the `gh` CLI on your computer. Nothing here is typed into chat.

**1. Create the private repo and its labels** (on your computer)
```bash
gh repo create petterfranzen/section9-hq --private --description "Section 9 task board"
for l in agent:batou agent:tachikoma agent:ishikawa agent:togusa agent:saito \
         status:todo status:working status:review status:done status:blocked; do
  gh label create "$l" -R petterfranzen/section9-hq
done
```

**2. Let the agents' token reach it.** github.com, Settings, Developer settings,
Fine-grained tokens, `agents-ghost`, Edit. Under *Repository access* add
`section9-hq`. Under *Repository permissions* set **Issues: Read and write**
(Metadata stays read-only). Change nothing else. Save.

**3. Copy the secrets Hetzner → VPS** (on your computer; the secrets pass through
the pipe and are never written to your disk)
```bash
ssh hetzner 'cd /var/backups/agent-manager-secrets && tar cf - hermes.env sandbox.env hermes_ssh_key sandbox_authorized_keys' \
  | ssh vps 'umask 077 && mkdir -p /etc/section9 && tar xf - -C /etc/section9 --numeric-owner'
ssh vps 'chown root:root /etc/section9/*.env && chmod 600 /etc/section9/*.env && ls -ln /etc/section9'
```
The listing must show four files: both `.env` files `-rw-------` owned by 0;
`hermes_ssh_key` `-r--------` owned by 10000.

**4. Install** (on the VPS: `ssh vps`)
```bash
git clone https://github.com/petterfranzen/agent-manager /opt/agent-manager
/opt/agent-manager/vps/install.sh
```
It stops at the first problem and says what is missing; fix that and run it again
(it is safe to repeat). It adds 4 GB of swap, builds and starts Hermes and the
sandbox, removes Hostinger's Hermes and Traefik (cloudflared stays), and starts
the two timers. The first build takes several minutes.

**5. Check** (on the VPS)
1. `python3 -I /opt/agent-manager/vps/dispatch.py status` shows
   `queue: 0 todo`, `spend: ok` and `hermes: reachable`.
2. `docker ps` lists exactly `section9-hermes-1` and `section9-sandbox-1`
   (plus cloudflared if it runs in Docker). No Hostinger or Traefik.
3. `ss -ltn | grep -E ':8642|:2222'` shows both on `127.0.0.1` only.
4. `systemctl list-timers 'section9-*'` shows both timers.
5. End to end, on your computer:
   ```bash
   gh issue create -R petterfranzen/section9-hq -t "Hello" -b "Reply with one line saying hi, then set the status to done." -l agent:ishikawa -l status:todo
   ```
   Within about 5 minutes the issue gets a comment from Ishikawa and the label
   `status:done` (closed). If it turns `status:blocked`, the dispatcher's comment
   says why. Logs: `journalctl -u section9-dispatch -n 40` on the VPS.

If step 5.5 works, tell the Major (then step 6). Then the Major moves the agents over one by one
(phase 3 of the plan).

**6. Pick up the HQ page, sync and spend (one time, after the VPS is already running)**

On the VPS, or in the darkstar session:
```bash
cd /opt/agent-manager && git pull && vps/install.sh
```
From then on `section9-sync.timer` (every 5 min) fast-forwards `/opt/agent-manager` to
`main` once all CI checks are green on that commit, and re-runs what changed: compose up
for changes to `vps/compose.yaml`, `sandbox/` or `hermes/`; the systemd units for changes
to `vps/systemd/`. It logs to the journal: `journalctl -u section9-sync -n 20`. It refuses
a checkout with local changes and stops retrying a commit after 3 failed applies.

**Auto-apply (8 Nov).** An ordinary merge to `main` needs no command on the VPS. The sync applies
the tip of `origin/main` when all of these hold:

1. it is a descendant of the commit last applied (fast-forward only; a force-pushed `main` is
   refused and reported, never applied);
2. every required check run in `ci.yml` (the `REQUIRED_CHECKS` list in `vps/sync.sh`) is completed
   with conclusion `success` on that exact SHA, read from the public GitHub API;
3. it has been on `main` for the soak, 30 minutes by the commit's time (the squash-merge time), so a
   bad merge that is reverted or fixed inside that window never lands: only the tip ever applies;
4. it changes none of the **protected set**, or each protected file it changes is pinned (below).

The protected set is short and explicit (`PROTECTED` at the top of `vps/sync.sh`): `vps/sync.sh`,
`vps/install.sh`, `vps/install-units.sh`, `vps/systemd/section9-sync.*` (the units that run the sync),
`.github/workflows/` (CI is the green light the sync trusts) and any path with `sudoers` in its name.
These can widen what a merge does as root or weaken the gate, so they are pinned by sha256 in the
root-only `/etc/section9/pins.toml`. A commit that changes one waits until you have read the change and run
```bash
sudo /opt/agent-manager/vps/sync.sh --accept   # fetches and pins the protected files of origin/main
```
It stops instead, and says so on one open issue titled **Sync** in `section9-hq` (created the
first time it has something to say); a dirty checkout and a failed apply are reported on the
same issue. Anything not in the set (compose, sandbox, hermes, the other units, dispatch.py,
docs) applies by itself. The defaults cannot be shortened by a file. The optional root-only
`/etc/section9/sync.toml` can add to them and change the soak (an unreadable file stops the sync
and says so on the issue):
```toml
# /etc/section9/sync.toml -- optional
soak_minutes = 30                # 0 to 1440
protect = ["vps/hq-status.py"]   # extra repo-relative prefixes that need --accept
```
Hermes mounts `hermes/config.yaml` from the checkout, so an auto-applied config change still needs
`docker compose -f /opt/agent-manager/vps/compose.yaml restart hermes` (see "Applying it" below).

**Timer health without SSH.** Every tick the sync keeps the **Sync** issue current (it rewrites it
when something changed, and at least hourly). Besides what the VPS did with itself it lists, for
`section9-routines.timer` and `section9-dispatch.timer`: `systemctl is-active`, the last trigger
time and how long ago, the last service result, and the last error-looking line of the service
journal. A timer that is not active or has not fired for over 30 minutes is marked `STALLED`.
A malformed `/etc/section9/routines.private.toml` no longer stops the routines: `dispatch.py routines`
logs `private routines not loaded (...)` with the parse position only (never the file's text),
skips a private routine that lacks `agent`, `title` or `cron`, and goes on with the public ones.
The Sync issue shows that line.
The dispatcher's own limits come from the same directory, root-only, by hand:
```toml
# /etc/section9/limits.toml -- optional; without it the roster decides (as before)
preamble = """
You are read-only outside your task's repository: no change to /opt/agent-manager,
/etc/section9 or /var/lib/section9, and none to your own brief.
"""
cash_multiplier = 1.266  # top-level key, so it stays above every [table]. Optional: what OpenRouter usage
                         # costs in cash. Measured on the 6 Nov invoice: a $50 top-up cost $63.30 (5.5%
                         # credit fee + 20% UK VAT) = 1.266; with 25% Swedish VAT it would be 1.055 x 1.25
                         # = 1.319. Set, the Capacity and Spend issues show cash next to the usage figure;
                         # the cap and the fail-closed spend check still use usage as the key reports it.
[timeout_sec]
default = 3600      # a per-agent ceiling; agents/roster.toml can only lower it
Tachikoma = 10800

# Per-agent models (#101). Only these ids may run at all; agents/roster.toml may
# name any of them, and nothing else. An agent named here is pinned to that model
# (a one-line rollback: set it back to the default). Leave the file out and every
# agent runs the default.
[models]
allow = ["deepseek/deepseek-v4.1-flash", "anthropic/claude-sonnet-5.5", "moonshotai/kimi-k3.1-pro"]
# Saito = "deepseek/deepseek-v4.1-flash"     # roll Saito back to the default

# Per-agent monthly budget, USD, UTC month (#101). No entry: no guard.
[spend]
Saito = 8.0
Tachikoma = 5.0
```
**Per-agent models (#101).** The dispatcher sends the agent's model (from
`agents/roster.toml`, gated by `[models].allow` above) on every run, so the
switch needs no second gateway and no config change. `[models].<Name> = "<id>"`
pins that agent; setting it back to `deepseek/deepseek-v4.1-flash` is the whole
rollback. The ZDR-only provider list for each trial model lives in
`hermes/config.yaml` under `provider_routing.models`; the per-agent budget guard
reads `[spend]` and the ledger. Petter writes both tables (root-only) when he
pins the change; nothing in the repo can set them.

**Applying it (#101).** `hermes/config.yaml` is a bind mount, so `--accept` alone does not
reload it: restart Hermes after it, then check the gateway answers:
```bash
docker compose -f /opt/agent-manager/vps/compose.yaml restart hermes
curl -s http://127.0.0.1:8642/v1/models | head    # any answer (even 401 without the key) shows it is up
```
With no `[models]` table, or an agent whose resolved model is the default, the request is exactly
the old one (model `hermes-agent`, no `provider`); only an allow-listed non-default model sends its
id and `provider: openrouter`. There is **no auto-rollback**: if Hermes fails to start on the new
config, no agent runs until a revert PR is merged and `--accept` is run again. Enable `[models]`
only with Petter at the machine.

`/etc/section9/briefs/<Name>.md`, if it exists, is that agent's brief instead of the repo's.
`install.sh` also sets `/etc/section9/sandbox_authorized_keys` to `0644` root-owned (sshd in
the sandbox refuses the `0600` file).

A **second dispatcher lane** (section9-hq #79) is off unless the same root-only file says so:
```toml
lanes = 2               # 1 (default): one lane. 2: a second lane may run beside the first.
[pacing]
quiet_hours = 3.0       # after an unsafe reading, this long before the second lane comes back
```
`section9-dispatch-lane1.service`/`.timer` run `dispatch.py --lane 1` on the same 30-second cadence
as lane 0 and stop at once between runs, so with `lanes = 1` they cost nothing. Lane 1 takes the
oldest task it may run *beside* lane 0: a different agent, and a repo no open `status:working`
issue is in. The repo is the issue's `repo:<name>` label, else a repo name in its title or body
(agent-manager, section9-hq, ghost-in-the-machine, flight-tracker, nvim-trainer, dinner-planner,
portfolio); a task with neither — and a *running* task with neither — keeps lane 1 idle, so label
a task `repo:<name>` to let it run beside another. The repo-name match on the title and body is a best guess (the first repo named wins, and a passing mention counts), so a `repo:<name>` label is the reliable way. With `lanes = 2` lane 0 applies the same exclusion the other way round: it skips a task whose agent or repo lane 1 is running, and, while lane 1 is running anything, a task that names no repo; that exclusion stays on while lane 1 holds its run lock, so flipping `lanes` back to 1 mid-run does not open the gap. Both lanes sweep and pick under
`/var/lib/section9/dispatch.pick.lock`, so they never claim the same issue, and each has its own
run lock (`/var/lib/section9/dispatch.laneN.lock`). **Deploying a change to `dispatch.py` by hand:**
wait for any run started by the old code to finish first — the run-lock name changed from
`dispatch.lock` to `dispatch.lane0.lock`, so the new code would not see an old run's lock. The timers
cannot double-run on their own: each lane's unit is a `oneshot` and cannot start again while its run
is active. While `lanes` is 1 the lane-1 timer fires every 30 s but logs nothing (it exits at
once); `dispatch.py status` prints the lane count.

Lane 1 stands itself down, and says so in the journal (`lanes: lane 1 stands down: ...`), when
CPU steal is over `[pacing] steal_pct` (Hostinger's limit is on) or the runs of the last 24 h drew
over the 8,100-credit daily guidance in [CAPACITY.md](CAPACITY.md). It returns to two lanes only
after `[pacing] quiet_hours` of safe readings; `/var/lib/section9/lanes.json` holds that latch.
The **Capacity** issue reports runs and busy hours per lane, and the hours two ran at once.

**hq.sectionnine.dev** is Hermes's web dashboard (Ghost in the Shell theme), wired by hand once in
Cloudflare Zero Trust:
- Networks, Tunnels, `section9-vps`, Public hostname: `hq.sectionnine.dev` → change the service to
  `HTTP` `http://localhost:9119` (it was `http://localhost:8090`; cloudflared runs on the host network).
  Save, then run `install.sh` (or wait for the sync timer) so the dashboard is up.
- Access, Applications: keep the self-hosted app for `hq.sectionnine.dev` with your email only. The
  dashboard can edit Hermes's `.env` and open a shell, so treat it like SSH: never add the service
  token or widen the policy. Without the Access app the dashboard would be public behind only its
  own login: don't change the hostname first.
- **Dashboard login** (second layer, made once by `install.sh`, never printed). Hermes v0.22.5 only
  engages its login for a non-loopback bind *or* a non-loopback `dashboard.public_url`; we bind
  `127.0.0.1` (host networking, so `0.0.0.0` would listen on every interface) and
  `dashboard.public_url` in `hermes/config.yaml` is what makes the login mandatory, so keep it set.
  `sudo grep DASHBOARD /etc/section9/hermes.env` shows `HERMES_DASHBOARD_BASIC_AUTH_USERNAME` and
  `_PASSWORD`. To rotate, delete those lines (and `_SECRET`) and re-run `install.sh`, then
  `docker compose -f /opt/agent-manager/vps/compose.yaml up -d hermes`.
- The crew page keeps running on `127.0.0.1:8090` for later public use. To look at it:
  `ssh -L 8090:localhost:8090 <vps>`.
- Themes: `section9-hq-crew` also copies `hq/*.yaml` from ghost-in-the-machine `main` into
  `/var/lib/section9/dashboard-themes` (mounted read-only in Hermes). Without `hq/ghost.yaml` it
  installs the starter in `vps/dashboard-themes/ghost.yaml`. `dashboard.theme: ghost` in
  `hermes/config.yaml` makes it the default. Refresh the dashboard after a change.

What serves the crew page: the `hq` container (Caddy, 64 MB, `127.0.0.1:8090` only) reads `/var/lib/section9/hq`.
Two timers fill that folder: `section9-hq-crew` (10 min) mirrors `crew/` from
ghost-in-the-machine `main` (public, no token), and `section9-hq-status` (2 min) writes the live
`status.json` from `section9-hq` issues, OpenRouter's usage (a percentage, no dollars) and
the model in `hermes/config.yaml`. Check: `curl -s localhost:8090/status.json | head`.

Private tasks show as "On a private task": any issue labelled `private`, and any title containing
a term in `/etc/section9/crew-deny.txt` (one per line, root-only). Without that file only agent
states and "On a task" are published. Create the file before you expect titles on the page.

The board: `https://hq.sectionnine.dev/board` is the waits page from ghost-in-the-machine
(`hq/board/index.html`, PR #18), vendored byte-for-byte into `vps/board/index.html` next to its
`board-sample.json` (see `vps/board/README.md` for the source sha and the file hashes). A fix goes
through a ghost-in-the-machine PR, a copy here over that pinned commit sha, then merge. `vps/board/` is not in the protected set, so `section9-sync` applies it after CI and the soak.
`section9-hq-status` then writes, under `board/` in the same folder, the live `board.json` the page
polls: the same `tasks` array as `status.json` (fields `id`, `title`, `agent`, `status`, `waits`,
`waitsText`, `url`, `pr`, `updated`) with the budget and model blocks. The `waits` column comes from
the issue's `waits:*` label (else `agent`), its line from the latest comment starting "Waits on ...",
and an issue labelled `private` gets no title, no link and no waits line (rules above). It keeps
`board/status.json` for the old placeholder page (`vps/board/placeholder.html`, served only when
`vps/board/index.html` is missing) and `board-sample.json` as the page's own fallback.
Petter's one-time step in Cloudflare: Zero Trust -> Networks -> Tunnels ->
the VPS tunnel -> Public hostnames -> add `hq.sectionnine.dev`, path `^/board` (a regex, anchored so that Hermes paths like /api/dashboard do not match) -> `HTTP` `localhost:8090`,
**above** the catch-all `hq.sectionnine.dev` -> `localhost:9119`; and check that the Access application
covers the whole hostname (not a path). Check: `curl -s localhost:8090/board/board.json | head`.

`section9-spend.timer` (hourly) keeps the open `Spend` issue in `section9-hq` current: month, cap,
left and today from OpenRouter, with an "As of" time. Pin it in the GitHub UI if the automatic pin
failed (the log says so). If "As of" is over 3 hours old, check `systemctl status section9-spend.timer`.

`section9-capacity.timer` (daily) rolls `/var/lib/section9/runs.jsonl` up into the open `Capacity`
issue and adds the two pace lines. The ledger, the CPU headroom numbers, the pacing switch and the
monthly forecast/budget-request procedure are in `docs/CAPACITY.md`. A run's cost is the OpenRouter
`usage_monthly` delta; the ledger write is best effort and never blocks a run. The ledger file is
written `0640` in a `0750` directory: it carries every run's cost and CPU draw, so it is not
world-readable. Those modes apply when the writer creates the file; it also best-effort chmods an
existing file, so a `runs.jsonl` left `0644` by an older build (or by hand) is corrected on the
next run.

`section9-sca.timer` (weekly, Monday 05:30 UTC) runs `vps/sca-watch.py`: it reads the version pins in the
repo (`sandbox/Dockerfile` ARGs and the `vps/compose.yaml` image tags) and this host's own docker, runc,
containerd and openssh-server versions, asks OSV (`api.osv.dev`) for advisories, keeps CVSS 7.0 or higher
or anything on the CISA KEV list, and rewrites the open **Supply-chain** issue in section9-hq (created on
first use; pin it in the GitHub UI if the automatic pin failed, as for Spend). Fixes and their dates come
from OSV; it prints no tokens. A run with nothing to report still writes a dated **Clear** line, so a quiet
issue means the timer has stopped, not that the stack is clean. Check it with
`python3 -I /opt/agent-manager/vps/sca-watch.py` and `systemctl list-timers 'section9-sca*'`.
**Optional monthly image layer.** The digest-pinned base and service images cannot be checked by version, so
for them install a scanner and run the scan by hand (or add `--images` to the unit): `sudo snap install trivy`
(or trivy's own apt repo) gives `python3 -I /opt/agent-manager/vps/sca-watch.py --images`, which scans the
running containers' images. Nothing is installed by the repo; without a scanner the issue says so.

Each run reads OpenRouter `/key` twice, before it starts and after it ends, and each read waits up
to 10 s, so a slow OpenRouter can delay a run's start by that much. The dispatcher picks
`priority:high` first, then unlabelled, then `priority:low`, oldest first within each group; with no
priority labels the pick is unchanged (oldest first). Aging keeps a run of high tasks from starving
the rest: a task that has waited 12 h moves up one level, and a `priority:low` task that has waited
48 h moves to the top, so it runs even while `priority:high` work exists. An issue whose `agent:<token>` names no agent
is blocked with a comment once nothing else is todo, so a typo surfaces instead of being ignored.
`agent:any` hands the issue to the least-recently-active agent that may take it, judged from the
board's 100 most recently updated issues: an agent older than that window counts as the one idle
longest.

**Later.** Update: `git -C /opt/agent-manager pull --ff-only && /opt/agent-manager/vps/install.sh`.
Stop everything: `systemctl disable --now section9-dispatch.timer section9-dispatch-lane1.timer section9-routines.timer`.
A routine opens its issue at its cron time unless an issue with the same title is still `status:todo` or `status:working` for that agent; one waiting in `review` or `blocked` no longer blocks it (6 Nov: a forgotten "Nightly build" issue in review had skipped three nights). A routine may set `priority = "high"` (the nightly build does): the issue is created with `priority:high`, so with the priority ordering pinned it starts as soon as the current run ends.
A private routine (rental watch) goes in the root-only `/etc/section9/routines.private.toml`
as `[[routines]]` with `agent`, `title`, `description`, `cron`, `timezone`.
Delete `/var/backups/agent-manager-secrets/` on Hetzner once the VPS works.

---

## Older: the Hetzner stack (steps 1-10 below are history)

## What the Hetzner VM runs now (from 31 Oct 2026)

**flight-tracker only** (its own unit, data and cloudflared), plus
`agent-manager-sync.timer`, which stays because the VM will later pull deploys
for agents' larger projects from it. Paperclip, Hermes, the sandbox, Postgres,
the spend and crew-status timers and the heal step are gone. Steps 1-10 below
describe how the stack was set up and now apply to the VPS (see
[migration-vps.md](migration-vps.md)), not Hetzner.

### The portfolio on the Hetzner VM (from 1 Nov 2026)

The VM also serves the portfolio: `www.pastabake.cloud` -> one nginx container
on `127.0.0.1:8081`, pulled by `agent-manager-sync` (docs/DECISIONS.md,
2026-11-01). Nothing is installed for it by hand: the compose file and the pull
step arrive with a merged agent-manager commit, and the sync's self-update
installs the new copy of itself.

**Petter's one-time steps in Cloudflare** (the tunnel runs from a token, so
these are dashboard entries, not a file on the box):

1. **Public hostname.** Zero Trust -> Networks -> Tunnels -> the VM's tunnel ->
   Public hostnames -> Add: `www.pastabake.cloud` -> `HTTP` ->
   `localhost:8081`. Leave `flight.pastabake.cloud` -> `localhost:8080` as it
   is. (The same rule is recorded at `deploy/cloudflared/portfolio-ingress.yaml`.)
2. **Apex redirect.** Zone `pastabake.cloud` -> Rules -> Redirect Rules ->
   Single Redirect: when the hostname is `pastabake.cloud`, dynamic redirect to
   `https://www.pastabake.cloud` (301, preserve path). **Confirm first what
   serves the apex today** — it resolves, proxied, to an origin that answered
   nothing when checked on 1 Nov; if a homelab record is behind it, this
   replaces that behaviour, so settle that before saving.
3. **Make the GHCR package public.** GitHub -> the `portfolio` package ->
   Package settings -> Change visibility -> Public, so the VM pulls without a
   token (no new secret; the sync reads the public GitHub API only).
4. Leave the homelab's DNS alone. `www` gets its record from the tunnel's public
   hostname; nothing else under `pastabake.cloud` changes.

After the first merged agent-manager commit that carries `deploy/portfolio/`,
the portfolio comes up on its own within a few minutes. Watch it with:

```bash
journalctl -u agent-manager-sync -n 50 | grep portfolio
docker compose --project-name portfolio -f /opt/agent-manager/deploy/portfolio/compose.yaml ps
curl -fsS http://127.0.0.1:8081/healthz
```

The removal runs from `deploy/vm-sync.sh` once `deploy/DECOMMISSIONED` is in
the checkout. The sync logs each step to the journal
(`journalctl -u agent-manager-sync | grep decommission`), in order:
1. copy Paperclip's newest DB backup and `master.key` out of the
   `paperclip-data` volume to `/var/backups/paperclip/` (root-only, files older
   than 30 days are deleted on each run);
2. `docker compose down --volumes --rmi all --remove-orphans` (all four services,
   their volumes and images);
3. stop, disable and remove the spend and crew-status units and scripts and the
   heal step;
4. move the stack's files in `/etc/agent-manager/` (`postgres.env`,
   `paperclip.env`, `hermes.env`, `sandbox.env`, `paperclip-board.env`, the
   Hermes SSH key, the sandbox authorized keys, `bridge-keys/`, `crew-deny.txt`)
   to `/var/backups/agent-manager-secrets/` (dir 0700, files 0600, root-only,
   never pruned), because the VPS reuses these secrets and the providers can't
   show them again. Each move is logged. The sync needs none of them. Any other
   `.env` there is left and logged. Delete that directory by hand once the VPS
   has its own copies.

Each step leaves a marker in `/var/lib/agent-manager/decommission/`, so a re-run
does nothing twice and a failed step is retried on the next tick. To redo one,
delete its marker. The old sync copy applies the merge commit first (it still
runs `compose up`), then installs this one; the decommission starts on the tick
after. If the old copy cannot apply (Paperclip already down), run
`git -C /opt/agent-manager pull --ff-only && bash /opt/agent-manager/deploy/install-sync.sh` by hand.

After it ran, in Cloudflare Zero Trust (not done by the sync): remove the public
hostname `hq.sectionnine.dev` -> `localhost:3100` from the tunnel and the Access
application `section9-hq`. Keep the `claude-ops-stats` service token (flight-tracker
uses it). If cloudflared runs from a local `config.yml`, delete the `hq.sectionnine.dev`
ingress rule there instead.

For the Hetzner CX23 that runs flight-tracker (Ubuntu 26.04). Run everything as root in the
Hetzner web console, or over SSH once that works. About 30–45 minutes, mostly waiting.

**Before you start, you have:**
- the OpenRouter key (`sk-or-v1-…`, $40/month budget) in your password manager;
- the fine-grained GitHub token `agents-ghost` (`github_pat_…`), limited to
  `ghost-in-the-machine`, in your password manager;
- `ghost-in-the-machine` created (public, README, Pages from Actions, push
  protection, `protect-main` ruleset);
- 4 GB of swap on the VM (`swapon --show`).

**Don't start before 2026-10-27 04:10 UTC.** The pinned `postgres:17-alpine`
digest was published 2026-10-26 04:09 UTC (patch rule: 24 h).

## 1. Install Docker from Ubuntu's archive
Ubuntu's packages are patched nightly by unattended-upgrades, unlike Docker's
own repo.
```bash
apt-get update && apt-get install -y docker.io docker-compose-v2 git openssl openssh-client
systemctl enable --now docker
docker version --format '{{.Server.Version}}' && docker compose version
```

## 2. Get the repo
```bash
git clone https://github.com/petterfranzen/agent-manager /opt/agent-manager
cd /opt/agent-manager && git log --oneline -1
```

## 3. Create the secrets
```bash
bash /opt/agent-manager/scripts/new-secrets.sh
```
It asks for the OpenRouter key and the GitHub token and generates everything
else into `/etc/agent-manager/` (root-only). Running it again never overwrites
anything.

- Nothing is shown while you paste, but it reports how many characters it got.
  Compare that with the key's length in your password manager.
- **The Hetzner web console often garbles long pastes.** Paste each key in 2–3
  pieces, pressing Enter after each, then press Enter on an empty line to finish.
- Each key is checked live with OpenRouter and GitHub before it's saved. A
  garbled one is rejected with "Not accepted" and asked for again (up to 5 tries).
- Underscores in the GitHub token often arrive as another character or not at
  all (the console assumes a US keyboard). The script puts them back, so that's fine.

## 4. Start the stack
```bash
cd /opt/agent-manager && docker compose up -d --build
docker compose ps
free -m
```
All four services should be `running` (postgres `healthy`). The sandbox build
takes a few minutes the first time. `free -m` should still show about 1.5 GB or
more available.

Check that Hermes can reach the sandbox:
```bash
docker compose logs --tail 30 hermes
docker compose exec sandbox sh -c 'ls -l /home/agent/.git-credentials'   # exists, -rw-------
```

## 5. Publish the dashboard through Cloudflare
`sectionnine.dev` must be in the same Cloudflare account as the tunnel
(registering it with Cloudflare Registrar does that).

In **Cloudflare Zero Trust** (one.dash.cloudflare.com):
1. **Networks → Tunnels →** your tunnel **→ Public hostnames → Add**:
   subdomain `hq`, domain `sectionnine.dev`, service `HTTP` `localhost:3100`.
2. **Access → Applications → Add → Self-hosted**: name `section9-hq`, hostname
   `hq.sectionnine.dev` (no path, so the whole site is protected). Add two policies:
   - `petter`: action **Allow**, include **Emails** your address (for your browser);
   - `claude`: action **Service Auth**, include **Service Token** `claude-ops-stats`.

Check from outside: `https://hq.sectionnine.dev` should show the Cloudflare
login, not Paperclip.

## 6. Claim Paperclip
```bash
cd /opt/agent-manager && echo '{"$meta":{"version":1,"updatedAt":"2026-10-27T00:00:00Z","source":"configure"},"database":{"mode":"postgres"},"logging":{"mode":"file"},"server":{"deploymentMode":"authenticated"},"telemetry":{"enabled":false}}' \
| docker compose exec -T -u node paperclip sh -c \
  'cat > /tmp/bootstrap.json && cd /app && pnpm paperclipai auth bootstrap-ceo --config /tmp/bootstrap.json --base-url https://hq.sectionnine.dev; rm -f /tmp/bootstrap.json'
```
It prints a one-time invite URL. It uses the CLI bundled in the Paperclip
image. The CLI insists on a config file, but our container runs from
environment variables, so the command hands it a throwaway one in `/tmp` (the
database address comes from `DATABASE_URL`); the server never reads it.
Open the link in the browser where you're logged in to Cloudflare Access, sign up and claim the board. The link is a one-time
credential: don't paste it anywhere else. Then:
- create a company named `Section 9` (the roster looks it up by name);
- in instance settings, turn **telemetry off** if there's a switch.
- Paperclip's setup wizard then insists on a first agent and may only offer
  the Claude adapter. Create **Saito** with it, then **pause** him at once (a
  Claude agent runs inside the Paperclip container). The roster script in
  step 7 adopts him by name and switches him to the Hermes gateway.

## 7. Hire the agents
The team is code: [`agents/roster.toml`](../agents/roster.toml) lists the
company budget, each agent (role, brief, budget, avatar) and its routine.
`scripts/paperclip-apply.py` applies it to Paperclip through the API. It is
safe to re-run: it creates what is missing and changes only what differs. It
never deletes anything.

**a. Mint two board keys, once:** `roster-vm` for this script and
`major-cloud` for Claude (step 9). Each command runs on its own:
```bash
cd /opt/agent-manager
# 1. Log the CLI in. It prints an approval link and waits: open it where you're
#    signed in to hq.sectionnine.dev (swap a 127.0.0.1:3100 prefix for that) and approve.
docker compose exec -u node paperclip sh -c \
  'cd /app && pnpm -s paperclipai auth login --api-base http://127.0.0.1:3100 --no-browser'
# 2. Mint the keys. Each prints a pcp_board_... key once: put it in your password manager.
for n in roster-vm major-cloud; do docker compose exec -u node paperclip sh -c \
  "cd /app && pnpm -s paperclipai token board create --api-base http://127.0.0.1:3100 --name $n --ttl-days 365"; done
# 3. Log the CLI out, so no login session stays on the volume.
docker compose exec -u node paperclip sh -c 'cd /app && pnpm -s paperclipai auth logout --api-base http://127.0.0.1:3100'
```
The keys expire after 365 days (a Daybook reminder is set). Renew the same
way, then retire the old ones with `pnpm paperclipai token board revoke <id>`.

**b. Store `roster-vm`**, root-only, without an editor and out of shell
history. Paste the first line on its own, then the key when it asks:
```bash
unset K; read -rsp 'Paste the roster-vm key: ' K; echo
```
Then:
```bash
printf 'PAPERCLIP_BOARD_KEY=%s\n' "$K" > /etc/agent-manager/paperclip-board.env; unset K
chmod 600 /etc/agent-manager/paperclip-board.env; wc -c < /etc/agent-manager/paperclip-board.env   # 79
```
(Pasting both blocks at once makes `read` swallow the next line instead of the key.)

**c. Apply.** Look first, then do it:
```bash
cd /opt/agent-manager
python3 scripts/paperclip-apply.py --dry-run
python3 scripts/paperclip-apply.py
```
One line per action, for example `agent Saito: switch adapter claude_local ->
hermes_gateway`; `ok: Batou unchanged` means nothing to do. The script reads
the gateway key (`API_SERVER_KEY`) from `/etc/agent-manager/hermes.env` and
sends it only when it creates an agent, switches its adapter, or you pass
`--set-api-key` (use that after rotating the gateway key). Agents in Paperclip
but not in the roster are listed as "not in roster" and left alone.

The script also sets the budget policies: the company's and each agent's monthly
cap from the roster, with unpriced usage allowed (`unpriced_usage` in
`roster.toml`; Hermes reports tokens, not dollars, so Paperclip's default
`block` pauses everyone after one run). It then resumes the company and agents
paused only by a budget. A pause set by hand (Saito's) is left alone and
listed; if Paperclip doesn't say why something is paused, you get `paused
(budget?): <name>: resume it in Paperclip`. After pulling this change:
```bash
cd /opt/agent-manager && git pull && docker compose up -d
python3 scripts/paperclip-apply.py --dry-run
python3 scripts/paperclip-apply.py
```

Last, it gives each agent a Paperclip `task_bridge` key, so the agent can set
the status of and comment on its own issues with `pc-task` in the sandbox
(`docs/paperclip.md`, section 6). The key is written once to
`/etc/agent-manager/bridge-keys/<agent>.key` (mode 0400, owned by the sandbox
user, uid 10001) and the sandbox mounts that directory read-only. To rotate a
key, delete its file and re-run: the script revokes the old key and mints a
new one. `bridge key Batou: mint (new)` on the first run, `ok: bridge key
Batou` after.

The company must exist already (step 6) and be named `Section 9`.
Plain HTTP is fine for the gateway: Hermes and Paperclip share a network
namespace, so this is loopback.

## 8. Routines
They are in the roster and are created **paused** (`active = false`). After
the Major's first supervised run, set `active = true` in a PR and re-apply.
Togusa's old "Backup coordinator check" routine was replaced by "QA sweep";
archive the old one in Paperclip (the script never deletes a routine).
The script never resumes a paused agent: unpause Saito in Paperclip then too.

## 9. Give Claude access
1. Use the `major-cloud` key from step 7a.
2. In the Claude Code cloud environment's settings (environment menu → Edit),
   add the secrets `PAPERCLIP_URL` = `https://hq.sectionnine.dev` and
   `PAPERCLIP_API_KEY` = the key.
3. Tell Claude. It checks the API, then does a first supervised nightly run.

## 10. Hand deploys to the Major
Once this is installed, merging a PR to `main` is the whole deploy. A systemd
timer runs `agent-manager-sync` every 5 minutes, which:
- fetches `main` and does nothing if the VM already runs that commit;
- waits until every CI check on that exact commit has passed (the names it
  requires are listed at the top of `deploy/vm-sync.sh`);
- fast-forwards `/opt/agent-manager`, runs `docker compose up -d --build`
  (restarting Hermes if Paperclip's container was replaced), waits for
  Paperclip's health check, then runs `scripts/paperclip-apply.py`;
- posts one comment per deploy on the open `Deploys` issue in Paperclip
  (created on first use). A failed step is reported there too and is not
  rolled back; after 3 failures on the same commit it stops until a new commit
  lands. It also refuses to run if `/opt/agent-manager` has local changes or
  `main` was force-pushed;
- after a successful deploy, removes every image no container uses
  (`docker image prune -a -f`; a rollback re-pulls by digest) and build cache
  older than a week, and reports `Disk: N% used,
  N GB free` plus the reclaimed space in that comment, prefixed with a
  `WARNING` once the disk is 85% full. The installer also caps the journal at
  200 MB (`/etc/systemd/journald.conf.d/agent-manager.conf`);
- installs a second timer, `agent-manager-spend.timer` (hourly, at :05): it reads
  the OpenRouter key's real usage and books the spend since the last run in
  Paperclip per agent (cost events, split by the agents' token share), so the
  budgets in `roster.toml` enforce within the hour. Once a day, from 07:00
  Stockholm, it comments a summary on the "Spend" issue. Run it by hand with
  `systemctl start agent-manager-spend`; preview with
  `python3 -I /usr/local/sbin/agent-manager-spend --dry-run`;
- after each spend run, `agent-manager-crew-status.service` (pulled in by the
  spend service, ordered after it, failing on its own) publishes the public
  crew page's `crew/status.json` to ghost-in-the-machine's `crew-status`
  branch, which the page fetches from
  `https://raw.githubusercontent.com/petterfranzen/ghost-in-the-machine/crew-status/crew/status.json`.
  It reads Paperclip (agents, issues, runs, cost by agent), OpenRouter's month
  and merged PRs, writes through the contents API with the sandbox's
  `GH_TOKEN`, and skips the commit when only `generatedAt` changed in the last
  3 hours. Text is allowlisted (public issue titles and merged PR titles);
  every other task shows as "On a private task" / "Research (private)", and a
  private word anywhere fails the run before anything is written. The
  private words are one per line in root-only `/etc/agent-manager/crew-deny.txt`
  (mode 600); without it the run publishes nothing. Preview with
  `python3 -I /usr/local/sbin/agent-manager-crew-status --dry-run`; run with
  `systemctl start agent-manager-crew-status`. ghost-in-the-machine's CI and
  Pages only run on PRs and `main`, so the branch triggers nothing; never open
  a PR from it;
- re-runs the installer (`--from-sync`) when a deploy changed `deploy/`, and
  says so in the comment.

**Trust model.** Anyone who can merge to `main` can run code as root on the
VM. `main` is therefore protected by a ruleset that requires a PR and green CI,
and the sync only ever reads the public GitHub API: no inbound access, no new keys.

Install it once, as root:
```bash
bash /opt/agent-manager/deploy/install-sync.sh
```
It runs the first sync straight away, which applies what is checked out
(idempotent: unchanged containers and agents are left alone). The installer is
only needed once: the sync re-installs its own copy when `deploy/` changes.

Watch and pause it:
```bash
systemctl list-timers agent-manager-sync.timer
journalctl -u agent-manager-sync -n 50
cat /var/lib/agent-manager/sync-state.json
systemctl disable --now agent-manager-sync.timer    # pause; enable --now to resume
```
To run it by hand, use `systemctl start agent-manager-sync`.

## Day to day
| Task | Command |
|---|---|
| Status and memory | `cd /opt/agent-manager && docker compose ps && docker stats --no-stream` |
| Logs | `docker compose logs --tail 100 <service>` |
| Update after a merged PR | Nothing: the sync applies it within ~5 minutes of green CI (step 10). By hand: `systemctl start agent-manager-sync` |
| Stop everything (data kept) | `docker compose down` |
| Disk used by Docker | `docker system df` |
| CPU credits and the Hostinger limit | `python3 -I /opt/agent-manager/vps/dispatch.py cpu`; the figures are also on the hourly **CPU credits** issue in section9-hq |
| Run ledger and the month's pace | `tail -n 20 /var/lib/section9/runs.jsonl`; the roll-up is the daily **Capacity** issue. See `docs/CAPACITY.md` |
| Sync traffic (should be near zero when nothing runs) | `docker compose exec -u agent sandbox sh -c "awk '/eth0/{print \$2}' /proc/net/dev"` twice, ~10 s apart |

## Troubleshooting
- **CPU caps and the credit balance** (1 Nov): the sandbox is capped at 0.4 vCPU and Hermes at
  0.3 (`vps/compose.yaml`), one child agent at a time. A full 8,100-credit balance is 5 h at the
  cap; Tachikoma's 3 h night is about 60%. CPU-bound builds run ~2.5x slower, and dashboard pages
  opened during a run are slower. Three long nights in a row: raise the sandbox to 0.45 and cut
  the build, rather than lifting the cap. Change the caps in compose and in `SANDBOX_CORES` /
  `HERMES_CORES` in `vps/dispatch.py` together (CI checks compose). Roll back with
  `systemctl disable --now section9-cpu.timer` (snapshot only) or by editing compose back.
- **A leftover `skills/impeccable` on the Hermes volume** is deleted by `install.sh` and by
  the sync after a compose/Hermes change; by hand:
  `docker compose exec -T hermes rm -rf /opt/data/skills/impeccable`.
- **Paperclip says the Hermes gateway is unreachable** (runs fail with
  `hermes_gateway_connect_failed`, ECONNREFUSED 127.0.0.1:8642): the sync's
  heal step restarts Hermes by itself after two refused checks in a row, at
  most every 30 minutes, and notes it on the Deploys issue. Its count is in
  `/var/lib/agent-manager/hermes-probe.json`. By hand: `docker compose logs hermes`.
  The gateway must listen on 127.0.0.1:8642 inside the shared namespace. Restart
  both: `docker compose restart paperclip hermes`.
- **Hermes can't SSH to the sandbox:** check the key mount
  (`docker compose exec hermes ls -l /opt/data/ssh/`, owner 10000, mode 0400)
  and `docker compose logs sandbox`. A "host key changed" error after deleting
  the `sandbox-hostkeys` volume means Hermes's known_hosts needs that entry
  removed: `docker compose exec hermes ssh-keygen -R sandbox`.
- **Memory tight** (flight-tracker slowing, swap in use): `docker stats`. Lower a
  container's `mem_limit` or stop the stack; flight-tracker keeps running either way.
- **Every agent paused, budget incidents that won't resolve** ("Completed runs
  must finish accounting before resuming"): a run ended without its usage
  receipt (`process_lost` after a restart or OOM kill, or cancelled), so
  Paperclip keeps its accounting pending and hard-stops the budgets. There is
  no API to close it. The sync fixes it by itself every 5 minutes, an hour
  after the run ended (`/usr/local/sbin/agent-manager-heal-accounting`,
  `deploy/heal-accounting.py`; its lines in `journalctl -u agent-manager-sync`
  start with `heal:`). To do it at once, run it by hand:
  `/usr/local/sbin/agent-manager-heal-accounting`. Its SQL is the only write to
  Paperclip's database outside Paperclip: it marks only receipt-less
  `process_lost`/cancelled runs as accounted and releases their held
  reservations. The real money is booked by the spend sync anyway. Then it
  resolves the open hard-stop incidents at the same amount; Paperclip refuses
  any whose booked spend has reached its budget.
- **An agent stuck "pausing"** (status paused, `lifecycleState` "pausing", resume
  does nothing): a cancelled run never got its cancellation acknowledged, so
  Paperclip waits for stop proof for ever. There is no API for it. The same
  heal step marks such runs' cancellation acknowledged once the agent has been
  pausing for an hour and the run ended an hour ago with no process and no live
  lease, then asks Paperclip to resume the agent (lines start `heal:`). If the
  resume doesn't take, `POST /api/agents/{id}/resume` once the agent shows
  lifecycle "paused".
- **Why a run's process was lost:** `docker inspect -f '{{.Name}} oom={{.State.OOMKilled}} restarts={{.RestartCount}} started={{.State.StartedAt}}' $(docker compose ps -q)`
  and `journalctl -k --since '<time>' | grep -i -E 'oom|killed process'`.
- **A browser hangs or the sandbox is short of memory:** `shoot` kills its
  browser after 90 s. Anything else: `docker compose exec -u agent sandbox pkill -x chrome-headless`.
