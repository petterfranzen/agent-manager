# Runbook: deploy the agent stack on the VM

## Section 9 on the VPS: Monday 2 Nov (do these in order)

Paperclip is gone. On the VPS, Hermes and the sandbox run from `vps/compose.yaml`;
`dispatch.py` (systemd timers, every 2 min) feeds them GitHub issues from the
private repo `section9-hq`. Plan and reasons: [migration-vps.md](migration-vps.md).
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

If step 5.5 works, tell the Major. Then the Major moves the agents over one by one
(phase 3 of the plan).

**Later.** Update: `git -C /opt/agent-manager pull --ff-only && /opt/agent-manager/vps/install.sh`.
Stop everything: `systemctl disable --now section9-dispatch.timer section9-routines.timer`.
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
- the OpenRouter key (`sk-or-v1-…`, $20/month budget) in your password manager;
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

## Troubleshooting
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
