As of 2026-10-28.

# Paperclip plugins and the Cloudflare sandbox

## Bottom line

- Paperclip's plugin system is real but marked alpha by its own docs. Plugin code is trusted code, not sandboxed.
- The Cloudflare sandbox plugin exists (official, `@paperclipai/plugin-cloudflare-sandbox`). It cannot serve our agents: `hermes_gateway` is not an adapter that Paperclip lets use sandbox environments. **Skip it.**
- Nothing in the plugin ecosystem is worth installing on our VM today. Details and a short watch list are at the end.

## Sources and how to read the citations

- `PC:<path>` means `https://github.com/paperclipai/paperclip/blob/v2026.1023.0/<path>`. Tag `v2026.1023.0` is commit `1b4c803816e82b0265c8422e1a804327a7ed8c54` and is our image version.
- Caveat: the local clone is a detached checkout of that tag (git log shows one commit, dated 2026-10-11), not main of 2026-10-27. Anything added after the tag is not read here. npm shows newer nightly (2026.1027.0) and canary (2026.1028.0-canary.6) builds. [npm registry, `@paperclipai/plugin-cloudflare-sandbox`](https://registry.npmjs.org/@paperclipai/plugin-cloudflare-sandbox)
- GitHub API access was not enabled in this session, so release and issue data comes from github.com pages and npm.

## How plugins work

**Own status.** "It is intentionally narrower than PLUGIN_SPEC.md ... this guide only covers the alpha surface that exists now." PC:doc/plugins/PLUGIN_AUTHORING_GUIDE.md (lines 3-5). The SDK README says "the runtime deployment model is still early". PC:packages/plugins/sdk/README.md (line 40). Sandbox-provider plugins get an "Experimental" badge in the UI. PC:ui/src/pages/PluginManager.tsx (lines 56, 60). The Environments feature itself is behind an experimental toggle, `enableEnvironments`. PC:ui/src/pages/InstanceExperimentalSettings.tsx (line 205).

**What a plugin can do.**
- Event hooks: subscribe to core events such as `issue.created`, `agent.run.finished`, `approval.decided`, `cost_event.created`. PC:packages/plugins/sdk/README.md (Events section).
- Cron jobs, inbound webhooks, own JSON API routes under `/api/plugins/:id/api/*`, plugin state, an own database namespace with migrations. PC:packages/plugins/sdk/README.md; PC:doc/plugins/PLUGIN_AUTHORING_GUIDE.md.
- Agent tools (`agent.tools.register`); invoking agents and chat sessions; creating or updating issues; managed agents, projects, routines and skills. PC:packages/plugins/sdk/README.md (Capabilities table); PC:packages/plugins/plugin-llm-wiki/src/manifest.ts (capabilities list).
- Execution environments: `environment.drivers.register` lets a plugin declare a `sandbox_provider` driver. PC:packages/plugins/sdk/README.md; PC:doc/plugins/SANDBOX_PROVIDER_CAPABILITIES.md.
- UI: pages, sidebar items, dashboard widgets, detail tabs, toolbar buttons and an always-mounted overlay. PC:packages/plugins/sdk/README.md (UI slots table).
- Not the same thing: **adapters** (agent runtimes such as `hermes_gateway`) are a separate extension point, installed as "external adapters" through the Adapter manager. PC:docs/adapters/external-adapters.md. "Apps" in the UI (`ui/src/pages/apps/`) are MCP-based connections (Composio, Arcade, Google Sheets), an internal, experimental connections system. There is no public plugin marketplace: the spec lists "a public marketplace" as out of scope. PC:doc/plugins/PLUGIN_SPEC.md (lines 59-62); PC:doc/connections/README.md. ClipHub (company templates) is a plan document, not plugins. PC:doc/CLIPHUB.md. Community plugins are listed in [awesome-paperclip](https://github.com/gsxdsm/awesome-paperclip) and on npm.

**Where plugin code runs.**
- Worker: one Node child process per plugin, forked by the Paperclip server, JSON-RPC over stdio. PC:server/src/services/plugin-worker-manager.ts (`fork(` at line 2936); PC:doc/plugins/PLUGIN_SPEC.md (section 12.1, lines 447-456).
- The worker gets a minimal environment, not the server's `process.env`, so `DATABASE_URL` and similar do not leak to it. PC:server/src/services/plugin-worker-manager.ts (lines 2924-2934). Only the first-party sandbox providers receive their documented API-key variable, and only from a trusted install origin. PC:server/src/services/plugin-loader.ts (lines 121-195).
- Plugin UI: JavaScript loaded into the main Paperclip page, same origin, with the board session. "Manifest capabilities are not a frontend sandbox." PC:packages/plugins/sdk/README.md (lines 42-43).
- So workers are separate processes, not separate containers: they share the Paperclip container (our 768 MB cap, an inference from DESIGN.md) and its filesystem. Each enabled plugin adds one Node process.
- Remote part: only what a plugin itself calls (a Cloudflare Worker, a SaaS API). The host does not run plugins remotely.

**Permissions.** Capabilities are declared in the manifest and checked on each host call (`plugin-capability-validator.ts`). Docs say plainly: "Treat plugin workers and plugin UI as trusted code." Capabilities are not a sandbox for untrusted marketplace code. PC:doc/plugins/PLUGIN_AUTHORING_GUIDE.md (lines 9-18). A plugin that gains capabilities in an upgrade is parked in `upgrade_pending` until an operator approves. PC:doc/plugins/DISTRIBUTION-PLUGINS.md (Selection, upgrades and rollback).

**Install and enable in Docker.**
- Install is an imperative admin action: UI Plugins page, `paperclipai plugin install <npm-package> [--version X]`, or `POST /api/plugins/install {packageName, version}`. The route needs instance admin. PC:server/src/routes/plugins.ts (lines 1141-1145); PC:cli/src/commands/client/plugin.ts (lines 396-412).
- Under the hood the server runs `npm install <pkg>@<version> --prefix <dir> --save --ignore-scripts` at runtime. So it needs `npm`, registry network access and a writable volume. PC:server/src/services/plugin-loader.ts (lines 1241-1259); PC:packages/plugins/sdk/README.md (line 49).
- Install state lives in Postgres plus `~/.paperclip/instances/default/plugins/` (under `PAPERCLIP_HOME`, our data volume). PC:doc/plugins/PLUGIN_SPEC.md (lines 234-237).
- Our default image only ships source for the sandbox providers, with no `dist`. The self-hosted auto-install list is just `kubernetes`. PC:Dockerfile (lines 236-245); PC:server/src/services/bundled-plugins.ts (line 114).
- A declarative path exists: an image catalog (`distribution/catalog.json`, per-bundle sha256 digest checked at boot) plus `plugins.autoInstall`. It means building a derived image, and the doc says the catalog alone does not enable plugins on self-hosted instances. PC:doc/plugins/DISTRIBUTION-PLUGINS.md.
- Hardening lever: `PAPERCLIP_HIDDEN_SETTINGS=instance.plugins` makes install, upgrade and uninstall return 403 afterwards. PC:docs/deploy/environment-variables.md (lines 83-95); PC:server/src/routes/plugins.ts (lines 102-108).

**Pinning.**
- Yes by version: `--version` or `name@1.2.3`; the first-party plugins are released in lockstep with Paperclip (`2026.1023.0` on npm for plugin-sdk, cloudflare, daytona, e2b, kubernetes, modal and workspace-diff). [npm registry](https://registry.npmjs.org/@paperclipai/plugin-cloudflare-sandbox)
- No integrity pin on the npm path: no lockfile and no hash check, so transitive dependencies float. The Cloudflare plugin only depends on `@paperclipai/plugin-sdk`, pinned exactly in its published manifest. npm shows SLSA provenance attestations for the first-party packages, which Paperclip does not verify. Only the image-catalog route verifies a digest.
- Install by recreate-from-code needs a script run after boot (like our roster script). It is untested whether our board `PAPERCLIP_API_KEY` counts as instance admin.
- 24 h rule: `2026.1023.0` was published 2026-10-23T23:54Z, so it is old enough. Never use the `latest` tag blindly, and never `nightly` or `canary`.

## Plugins worth knowing

Dates are npm "latest" publish dates. "Alpha" is my label unless the source says otherwise.

| Plugin | What it does | Maintainer | Maturity | Runs where | Needs | Verdict for us |
|---|---|---|---|---|---|---|
| `@paperclipai/plugin-cloudflare-sandbox` 2026.1023.0 | Sandbox provider; runs adapter commands in Cloudflare containers via your own Worker bridge | Paperclip (official) | Experimental, first published 2026-05-11, 20+ releases, latest 2026-10-23 | Worker in Paperclip container; commands in Cloudflare | Cloudflare account on Workers Paid ($5/mo), `wrangler`, Docker to deploy, bridge bearer token | Skip (below) |
| `@paperclipai/plugin-daytona`, `-e2b`, `-modal`, `-kubernetes`, `-novita-sandbox`, `-createos`, `-exe-dev` | Other sandbox providers, same driver contract | Paperclip (official) | Experimental; daytona/e2b/modal/kubernetes 2026.1023.0 | Worker local; commands at provider | Provider account and API key | Skip; same adapter limit. Note Hermes has its own Daytona and Modal backends. |
| `@paperclipai/plugin-workspace-diff` 2026.1023.0 | "Changes" tab (git diff) on execution and project workspaces | Paperclip (official) | Alpha, first published 2026-05-18 | In Paperclip | Nothing; capabilities `ui.detailTab.register`, `execution.workspaces.read`, `project.workspaces.read` | Low value; our agents work inside Hermes, not Paperclip workspaces |
| `@paperclipai/plugin-llm-wiki` 0.1.0 | Local-file wiki, ingest and query, managed agent and routines | Paperclip (official) | Alpha; npm copy is stale (2026-05-11, never updated) and repo marks it `private: true` | In Paperclip | Wants a `claude_local` agent by default | Skip |
| `@vectorize-io/hindsight-paperclip` 0.4.2 | Long-term memory: recall before each run, retain each comment | Vectorize (Hindsight) | Beta-ish; 9 versions, 2026-10-12, MIT, provenance | In Paperclip, calls Hindsight | Hindsight Cloud key, or self-host with an OpenAI key | Avoid: sends task text to a third party |
| `paperclip-github-plugin` 0.18.0 | Two-way GitHub issue import, PR triage queue, agent GitHub tools | alvarosanchez (community) | Active; 87 versions, 2026-09-17, Apache-2.0, provenance | In Paperclip, calls GitHub | A GitHub token in Paperclip | Avoid: puts a token in the Paperclip container |
| `paperclip-plugin-telegram` 0.8.1, `-discord` 0.11.0, `-slack` 2.1.0 | Notifications and slash commands from chat apps | mvanhorn (community) | 18, 24 and 13 versions; Aug-Sep 2026 | In Paperclip | Bot tokens | Skip: DECISIONS.md rules out IM |
| `paperclip-plugin-acp` 0.7.1 | Runs Claude Code, Codex, Gemini CLI from chat platforms | mvanhorn | 2026-08-24, 12 versions | In Paperclip | Chat bot, CLI auth | Skip: not our model path |
| `paperclip-plugin-tickler` 0.20.4 | Cross-org "needs you" page | nickallevato | Very active; 40 versions since 2026-09-27 | In Paperclip (UI) | Nothing | Skip: one org, Claude does check-ins |
| `paperclip-plugin-avp` 1.0.3 | Advisory reputation signals | agentveil-protocol | 4 versions, last 2026-05-26 | In Paperclip, calls AgentVeil | External service | Skip |

Sources for the community rows: [awesome-paperclip](https://github.com/gsxdsm/awesome-paperclip); npm registry pages `https://registry.npmjs.org/<package>` for each name (checked 2026-10-28). I only read the READMEs of hindsight, github and tickler. The rest are judged from npm metadata and the awesome-list text.

## Cloudflare sandbox

**What exists.** `packages/plugins/sandbox-providers/cloudflare` (`@paperclipai/plugin-cloudflare-sandbox`, driver key `cloudflare`). It is a thin client plus a **Worker bridge you deploy yourself** (`bridge-template/`): a Worker with a `Sandbox` Durable Object using `@cloudflare/sandbox` ^0.7.0, image `cloudflare/sandbox:0.7.0` plus git, curl, bash, `instance_type: standard-2`, `max_instances: 10`. The bridge exposes lease acquire, resume, release, destroy and exec under `/api/paperclip-sandbox/v1`, guarded by a bearer token (`BRIDGE_AUTH_TOKEN`). PC:packages/plugins/sandbox-providers/cloudflare/README.md; PC:packages/plugins/sandbox-providers/cloudflare/bridge-template/README.md; PC:packages/plugins/sandbox-providers/cloudflare/bridge-template/wrangler.jsonc; PC:packages/plugins/sandbox-providers/cloudflare/src/manifest.ts.

**Why it does not fit our setup (the decisive finding).**
- Paperclip only lets certain adapters use environments: `claude_local`, `codex_local`, `paperclip_runner`, `cursor`, `gemini_local`, `grok_local`, `kimi_local`, `opencode_local`, `pi_local`. Every other adapter is limited to `local`. `hermes_gateway` and `hermes_local` are not in the list. PC:packages/shared/src/environment-support.ts (lines 67-93).
- Even if they were: `hermes_gateway` only makes HTTP calls to a Hermes server. The commands run inside Hermes, in our SSH sandbox. PC:server/src/adapters/hermes-gateway-doc.ts. Paperclip never sees the shell commands.
- Hermes picks its own terminal backend: local, docker, ssh, singularity, modal, daytona, vercel_sandbox. No Cloudflare. [Hermes docs, tools](https://hermes-agent.nousresearch.com/docs/user-guide/features/tools). Switching our sandbox would therefore be a Hermes setting (Daytona, Modal or Vercel), not a Paperclip plugin.
- If we moved the coder to `opencode_local` (listed as a later option in DESIGN.md), the plugin would apply. But then the CLI runs inside the Cloudflare container, so the OpenRouter key has to be in the container's environment. That breaks our "sandbox never holds the model key" rule. Bridge `exec` passes env vars as shell variables per command. PC:packages/plugins/sandbox-providers/cloudflare/bridge-template/src/exec.ts (`buildLoginShellScript`).

**How commands would run, if it applied.** Paperclip leases a container per run (or reuses one with `reuseLease` plus `keepAlive`), syncs the workspace in, runs the adapter CLI there, syncs results out. Sandbox code calls back to Paperclip through an HTTP bridge, so Paperclip needs a URL the container can reach. For us that is `hq.sectionnine.dev` behind Access (unverified how the callback authenticates). PC:packages/plugins/sandbox-providers/SANDBOX-REQUIREMENTS.md (Security boundary).

**Isolation compared with our SSH container.**
- Cloudflare: each container "runs in a Firecracker microVM with its own kernel and network". Disk is ephemeral. [Cloudflare Containers architecture](https://developers.cloudflare.com/containers/platform-details/architecture/)
- Ours: a Docker container on the same VM, 1.5 GB cap, shared kernel with Paperclip and flight-tracker. A microVM on someone else's hardware is a stronger boundary from our production app. But the Cloudflare account becomes a new trust anchor holding bridge token, deploy rights and the repo token.
- Cloudflare's own guidance: "Once a value is in the sandbox, all the code there can read it." It recommends keeping credentials in the Worker and injecting them through an outbound handler. It also says a Container starts with `enableInternet: false` by default. [Cloudflare Sandbox security](https://developers.cloudflare.com/sandbox/concepts/security/) The bridge template sets neither internet nor an outbound handler, so I could not confirm that `git clone` works unmodified, or how a GitHub token would be injected. That would need a spike.
- Paperclip's own contract: the provider enforces isolation and egress policy; Paperclip "does not enforce this policy inside the sandbox". PC:packages/plugins/sandbox-providers/SANDBOX-REQUIREMENTS.md.

**Cost (Cloudflare pricing page, checked 2026-10-28).** Workers Paid is $5/month. It includes 25 GiB-hours memory, 375 vCPU-minutes and 200 GB-hours disk. Overage: $0.0000025 per GiB-second, $0.000020 per vCPU-second, $0.00000007 per GB-second. Memory and disk bill on provisioned size while the container runs; CPU on active use only; granularity 10 ms; charges stop when the container sleeps. Instances: `basic` 1/4 vCPU, 1 GiB, 4 GB; `standard-1` 1/2 vCPU, 4 GiB, 8 GB; `standard-2` (template default) 1 vCPU, 6 GiB, 12 GB. Workers, Durable Objects and optional logs bill separately (small at this volume). [Containers pricing](https://developers.cloudflare.com/containers/pricing/); [Sandbox pricing](https://developers.cloudflare.com/sandbox/platform/pricing/).

My arithmetic for about 62 container-hours a month (30 nights x 2 h, plus about 2 h of short tasks):

| Instance | CPU active 25% | CPU active 100% |
|---|---|---|
| `standard-2` (template default) | $3.9 + $5 plan = about $9 | $7.3 + $5 = about $12 |
| `basic` | about $1 + $5 = about $6 | about $1 + $5 = about $6 |

The plugin's default `sleepAfter` is 1 h, so a lease that is not destroyed can bill an idle hour per run. That is about 130 hours a month and about $9 to $16 on `standard-2`, plus $5. Against that, the SSH container costs nothing extra on the VM we already pay for. The $20 model budget is separate, but $6 to $21 a month for the sandbox would be a large share of what we spend overall.

**Latency.** Cloudflare says cold starts are "often" 1 to 3 seconds (image size dependent), and Durable Objects and containers may not be co-located. [Architecture page](https://developers.cloudflare.com/containers/platform-details/architecture/) Every command also passes Hetzner, then the bridge Worker, then the container. Our SSH hop is on the same VM. The plugin's defaults allow 300 s per command and 300 s for the bridge request. PC:packages/plugins/sandbox-providers/cloudflare/src/config.ts. For a 2-hour build the latency is irrelevant, but it adds up for many small commands.

**Fit with principles.**
- Recreatable from code: partly. Worker, image and `wrangler.jsonc` are code, but the account, secret (`wrangler secret put`) and plugin install are manual or scripted steps. A new account and token appear in the new-secrets script.
- Pinned and 24 h old: yes for the plugin and `@cloudflare/sandbox`/image tags in the template; the template uses `^` ranges, so we would pin them ourselves.
- Cheap: no (see above).
- ZDR: model traffic is unaffected (it goes Hermes to OpenRouter), but code, repo contents and command output would pass through Cloudflare. I did not find a Cloudflare statement on retention for Containers. Workers Logs are billed and optional, so keep them off.
- No xAI: not relevant.
- Maturity: sandbox plugins carry an Experimental badge; the template says `.workers.dev` previews are out of scope; Cloudflare's pages I read do not state beta or GA for Containers or the Sandbox SDK, though the DO scheduling policy it uses is "public beta".

**Verdict: skip.** It cannot be used by `hermes_gateway`, the one runtime we run. It does not replace the SSH sandbox, and a complement would add cost and a second credential holder for no gain. Revisit only if (a) the coder moves to `opencode_local` and (b) the model-key-in-sandbox rule is deliberately relaxed or Cloudflare's outbound-handler injection is shown to cover it. If we want a hosted sandbox for Hermes, test Hermes's own `daytona` or `modal` backend instead; I did not research their cost or retention.

## Recommendations

1. **Install no plugin now.** Top recommendation. Nothing found fits our adapter, keys and budget rules, and every plugin adds a Node process inside a 768 MB container plus trusted code with board-session UI access.
2. **If Petter still wants to try one:** `@paperclipai/plugin-workspace-diff@2026.1023.0`, the least risky official option (read-only capabilities, matches our image version, no accounts). Check first whether our agents ever create execution workspaces, or it will show nothing. After testing, set `PAPERCLIP_HIDDEN_SETTINGS=instance.plugins` so the install surface closes again.
3. **Watch, do not try yet:** the Cloudflare plugin, for the case where we move to `opencode_local` (see above); and Hermes backend changes (`vercel_sandbox`, `daytona`, `modal`), the real place to swap the sandbox.

**Avoid.**
- Anything installed from `latest`, `nightly` or `canary` without a pin and 24 h age.
- Plugins that take third-party tokens or send task text out: hindsight (Hindsight Cloud, or OpenAI key to self-host), GitHub Sync (GitHub token in Paperclip).
- Community plugins with `^` SDK ranges and no review. A plugin worker and its UI act with trusted access, and our Paperclip admin session (behind Cloudflare Access) is what the UI code would borrow.
- Treating `plugins.autoInstall` or the image catalog as a self-hosted switch. They are written for managed instances.
- Running `Environments` (experimental) on the production Paperclip without a throwaway instance first.
