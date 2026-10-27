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
2. **Access → Applications → Add → Self-hosted**: name `agents`, hostname
   `hq.sectionnine.dev` (no path, so the whole site is protected). Add two policies:
   - `petter`: action **Allow**, include **Emails** your address (for your browser);
   - `claude`: action **Service Auth**, include **Service Token** `claude-ops-stats`.

Check from outside: `https://hq.sectionnine.dev` should show the Cloudflare
login, not Paperclip.

## 6. Claim Paperclip
```bash
docker compose logs paperclip 2>&1 | grep -i -m1 board-claim
```
Open that link (at `https://hq.sectionnine.dev/...`), sign up and claim the
board. Then:
- create a company, e.g. `pastabake`;
- in instance settings, turn **telemetry off** if there's a switch.

## 7. Hire the agents
For each brief in [`agents/`](../agents/README.md): **Agents → Add agent →
adapter Hermes gateway**, with:

| Field | Value |
|---|---|
| Name / title | from `agents/README.md`, e.g. `Tachikoma` / `Nightly builder` |
| API base URL | `http://127.0.0.1:8642` |
| API key | the gateway key: `grep API_SERVER_KEY /etc/agent-manager/hermes.env` |
| Paperclip API URL | `http://127.0.0.1:3100` |
| Session key strategy | `issue` |
| Instructions | the agent's brief, pasted |
| Monthly budget | from the table in `agents/README.md` |

Plain HTTP is fine here: Hermes and Paperclip share a network namespace, so
this is loopback.

## 8. Routines
- **Nightly build:** cron `0 1 * * *`, Europe/Stockholm, assigned to Tachikoma (nightly builder).
- **Ops report:** `30 6 * * *`, assigned to Saito (ops).
- **Backup coordinator:** `0 */6 * * *`, assigned to Togusa (backup coordinator).

## 9. Give Claude access
1. In Paperclip, create a **board API key** for Claude.
2. In the Claude Code cloud environment's settings (environment menu → Edit),
   add the secrets `PAPERCLIP_URL` = `https://hq.sectionnine.dev` and
   `PAPERCLIP_API_KEY` = the key.
3. Tell Claude. It checks the API, uploads the avatars from `agents/avatars/`,
   then does a first supervised nightly run.

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
