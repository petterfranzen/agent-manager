#!/usr/bin/env bash
# section9-health: run scripts/health-run.sh in the sandbox image, in two containers.
# The first builds the five repos with GH_TOKEN blanked, since
# `npm ci` runs every dependency's install scripts, and prints the issue body. The
# second gets the agents' own GH_TOKEN (sandbox.env), builds nothing, and posts that
# body with `gh api`. --entrypoint sidesteps the image's sshd (and the entrypoint that
# stores the tokens); --rm throws each container, its clones and node_modules away.
# Root, from section9-health.service. Exit: 1 when a repo FAILs or nothing was posted.
set -euo pipefail

repo=${S9_REPO_DIR:-/opt/agent-manager}
run=(docker compose -f "$repo/vps/compose.yaml" run --rm --no-deps -T
     --user agent -e HOME=/home/agent -w /home/agent/work
     -v "$repo/scripts/health-run.sh:/usr/local/bin/health-run.sh:ro"
     --entrypoint /usr/local/bin/health-run.sh)

body=$(mktemp)
trap 'rm -f "$body"' EXIT
"${run[@]}" -e GH_TOKEN= sandbox --body-only > "$body"
"${run[@]}" sandbox --post - < "$body"
