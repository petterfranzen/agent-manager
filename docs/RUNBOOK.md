# Runbook: deploy the agent stack on the VM

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

**a. Mint a board key, once.** Inside the Paperclip container:
```bash
cd /opt/agent-manager
docker compose exec -u node paperclip sh -c 'cd /app && pnpm paperclipai auth login --api-base http://127.0.0.1:3100 --instance-admin --no-browser && pnpm paperclipai token board create --api-base http://127.0.0.1:3100 --name roster --ttl-days 365'
```
The first command prints an approval link. Open it in the browser where you
are signed in to `hq.sectionnine.dev` and approve. The second prints the key
**once**; copy it now. The key expires after 365 days; mint a new one then
(`pnpm paperclipai token board revoke <id>` retires the old one).

**b. Store it**, root-only, with an editor (never on the command line, so it
stays out of shell history):
```bash
install -m 600 /dev/null /etc/agent-manager/paperclip-board.env
nano /etc/agent-manager/paperclip-board.env     # one line: PAPERCLIP_BOARD_KEY=...
```

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

The company must exist already (step 6) and be named `Section 9`.
Plain HTTP is fine for the gateway: Hermes and Paperclip share a network
namespace, so this is loopback.

## 8. Routines
They are in the roster and are created **paused** (`active = false`). After
the Major's first supervised run, set `active = true` in a PR and re-apply.
The script never resumes a paused agent: unpause Saito in Paperclip then too.

## 9. Give Claude access
1. Mint a **board API key** for Claude the way step 7a does, with `--name claude`.
2. In the Claude Code cloud environment's settings (environment menu → Edit),
   add the secrets `PAPERCLIP_URL` = `https://hq.sectionnine.dev` and
   `PAPERCLIP_API_KEY` = the key.
3. Tell Claude. It checks the API, then does a first supervised nightly run.

## Day to day
| Task | Command |
|---|---|
| Status and memory | `cd /opt/agent-manager && docker compose ps && docker stats --no-stream` |
| Logs | `docker compose logs --tail 100 <service>` |
| Update after a merged PR | `git pull && docker compose up -d --build` |
| Stop everything (data kept) | `docker compose down` |
| Disk used by Docker | `docker system df` |

## Troubleshooting
- **Paperclip says the Hermes gateway is unreachable:** `docker compose logs hermes`.
  The gateway must listen on 127.0.0.1:8642 inside the shared namespace. Restart
  both: `docker compose restart paperclip hermes`.
- **Hermes can't SSH to the sandbox:** check the key mount
  (`docker compose exec hermes ls -l /opt/data/ssh/`, owner 10000, mode 0400)
  and `docker compose logs sandbox`. A "host key changed" error after deleting
  the `sandbox-hostkeys` volume means Hermes's known_hosts needs that entry
  removed: `docker compose exec hermes ssh-keygen -R sandbox`.
- **Memory tight** (flight-tracker slowing, swap in use): `docker stats`. Lower a
  container's `mem_limit` or stop the stack; flight-tracker keeps running either way.
