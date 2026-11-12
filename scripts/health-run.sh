#!/usr/bin/env bash
# Cross-repo health run: clone each of the five repos, run its build and its
# fast test commands, and rewrite one table in the open "Health run" issue in
# section9-hq (created and pinned on the first run).
#
#   scripts/health-run.sh --dry-run              print the table, post nothing
#   scripts/health-run.sh                        rewrite the issue (as the VPS does)
#   scripts/health-run.sh --only nvim-trainer    one repo (repeatable)
#   scripts/health-run.sh --repos-dir DIR        use checkouts you already have
#   scripts/health-run.sh --cmd repo='CMD'       override one repo's commands
#
# On the VPS it runs from section9-health.timer, inside the sandbox image: that
# is the agents' own toolchain and the agents' own token (GH_TOKEN in
# /etc/section9/sandbox.env). No other secret, no wider scope. The clones need
# no token at all (all five repos are public). See docs/RUNBOOK.md.
#
# This is a cheap cross-repo signal, not a second CI. What each repo runs lives
# in steps_for() below, one case per repo, and is what that repo's own CI or its
# README/CLAUDE.md runs. Keep it cheap: the heavy suites (Playwright, Maven)
# stay in the repos' own CI, and the table says so.
set -uo pipefail

org=${HEALTH_ORG:-petterfranzen}
board=${HEALTH_BOARD:-petterfranzen/section9-hq}
issue_title=${HEALTH_ISSUE_TITLE:-Health run}
repo_timeout=${HEALTH_TIMEOUT:-900}
log_lines=${HEALTH_LOG_LINES:-20}
repos=(ghost-in-the-machine nvim-trainer portfolio dinner-planner flight-tracker)

dry_run=0
repos_dir=""
only=()
overrides=()

usage() { sed -n '2,18p' "$0"; exit 0; }

while (( $# )); do
  case $1 in
    --dry-run)   dry_run=1 ;;
    --repos-dir) repos_dir=${2:?--repos-dir needs a directory}; shift ;;
    --only)      only+=("${2:?--only needs a repo name}"); shift ;;
    --cmd)       overrides+=("${2:?--cmd needs repo=command}"); shift ;;
    -h|--help)   usage ;;
    *) echo "unknown argument: $1 (try --help)" >&2; exit 2 ;;
  esac
  shift
done

# ---- what each repo runs -----------------------------------------------------
# $1 is the checkout, $2 a scratch directory for a build that writes outside the
# repo. One line per repo. The repo is PASS when this exits 0.
steps_for() {
  case $1 in
    ghost-in-the-machine) printf '%s' 'mkdir -p "$2" && python3 "$1/scripts/build-index.py" "$2" && test -s "$2/index.html"' ;;
    nvim-trainer)         printf '%s' 'cd "$1" && npm ci && npm run build' ;;
    portfolio)            printf '%s' 'cd "$1" && npm ci && npm run build' ;;
    dinner-planner)       printf '%s' 'cd "$1/backend" && npm ci && npm run build && cd "$1/frontend" && npm ci && npm run build' ;;
    flight-tracker)       printf '%s' 'cd "$1/frontend" && npm ci && npm run build' ;;
    *) return 1 ;;
  esac
}

describe_for() {
  case $1 in
    ghost-in-the-machine) echo 'build: `python3 scripts/build-index.py` (the CI index job)' ;;
    nvim-trainer)         echo 'build: `npm ci && npm run build`' ;;
    portfolio)            echo 'build: `npm ci && npm run build` (tsc, vite, the experiments list)' ;;
    dinner-planner)       echo 'build: backend and frontend, `npm ci && npm run build`' ;;
    flight-tracker)       echo 'build: frontend, `npm ci && npm run build`' ;;
    *) echo 'no suite yet' ;;
  esac
}

note_for() {
  case $1 in
    nvim-trainer)   echo '`playtest`/`playtest:edge` need a dev server and a browser' ;;
    portfolio)      echo '`smoke.spec.mjs` needs a preview server' ;;
    dinner-planner) echo 'the backend jest suite lands with the dinner-planner CI task' ;;
    flight-tracker) echo 'the Maven backend build and the Playwright suite' ;;
    *) echo '' ;;
  esac
}

override_for() {  # a --cmd repo=command wins over steps_for
  local want=$1 e
  for e in ${overrides[@]+"${overrides[@]}"}; do
    if [ "${e%%=*}" = "$want" ]; then printf '%s' "${e#*=}"; return 0; fi
  done
  return 1
}

# ---- small helpers -----------------------------------------------------------
human_time() {
  local s=$1
  if (( s < 60 )); then printf '%ds' "$s"; else printf '%dm %ds' $((s / 60)) $((s % 60)); fi
}

# Anything token-shaped in a log we are about to publish. By shape, so the token
# itself is never read, held or printed.
redact() {
  sed -E 's/(github_pat_[A-Za-z0-9_]{10,}|gh[pousr]_[A-Za-z0-9]{20,}|sk-or-v1-[A-Za-z0-9_-]{10,})/[redacted]/g'
}

selected() {  # is $1 in --only's list (or is the list empty)?
  (( ${#only[@]} == 0 )) && return 0
  local r
  for r in "${only[@]}"; do [ "$r" = "$1" ] && return 0; done
  return 1
}

work=$(mktemp -d "${HEALTH_TMPDIR:-${TMPDIR:-/tmp}}/health-run.XXXXXX")
trap 'rm -rf "$work"' EXIT

# ---- run ---------------------------------------------------------------------
rows=()      # repo|status|seconds|describe|note
failed=0
started=$(date -u +%s)

for r in "${repos[@]}"; do
  selected "$r" || continue
  describe=$(describe_for "$r")
  note=$(note_for "$r")
  steps=$(override_for "$r") || steps=$(steps_for "$r") || steps=""
  log="$work/$r.log"
  : > "$log"
  secs=0

  if [ -z "$steps" ]; then
    status=SKIP
    echo "no suite yet" > "$log"
  else
    src="$repos_dir/$r"
    if [ -n "$repos_dir" ] && [ ! -d "$src" ]; then
      status=SKIP
      echo "no checkout at $src" > "$log"
    else
      if [ -z "$repos_dir" ]; then
        src="$work/src/$r"
        mkdir -p "$work/src"
        echo "\$ git clone --depth 1 https://github.com/$org/$r" >> "$log"
        if ! timeout "$repo_timeout" git clone -q --depth 1 "https://github.com/$org/$r" "$src" >> "$log" 2>&1; then
          status=FAIL
          echo "could not clone $org/$r" >> "$log"
          rows+=("$r|$status|$secs|$describe|$note")
          failed=$((failed + 1))
          continue
        fi
      fi
      t0=$(date -u +%s)
      if timeout "$repo_timeout" bash -c "$steps" health-run "$src" "$work/out-$r" >> "$log" 2>&1; then
        status=PASS
      else
        status=FAIL
        failed=$((failed + 1))
      fi
      secs=$(( $(date -u +%s) - t0 ))
    fi
  fi
  rows+=("$r|$status|$secs|$describe|$note")

  if (( dry_run )); then
    case $status in PASS) printf 'ok   %-20s %s\n' "$r" "$(human_time "$secs")" ;;
                   FAIL) printf 'FAIL %-20s %s\n' "$r" "$(human_time "$secs")" ;;
                   *)    printf 'skip %-20s %s\n' "$r" "$(head -1 "$log")" ;; esac
  fi
done

total=$(( $(date -u +%s) - started ))

# ---- the table ---------------------------------------------------------------
body=$(mktemp "$work/body.XXXXXX")
{
  printf '**As of %s UTC** — five repos, whole run %s. Written by `scripts/health-run.sh`;\n' \
    "$(date -u '+%Y-%m-%d %H:%M')" "$(human_time "$total")"
  printf '`section9-health.timer` runs it nightly on the VPS, inside the sandbox image.\n\n'
  printf '| Repo | Result | Time | Ran |\n|---|---|---|---|\n'
  for row in ${rows[@]+"${rows[@]}"}; do
    IFS='|' read -r r status secs describe note <<<"$row"
    printf '| %s | %s | %s | %s |\n' "$r" "$status" "$(human_time "$secs")" "$describe"
  done
  printf '\n'

  notes=""
  for row in ${rows[@]+"${rows[@]}"}; do
    IFS='|' read -r r status secs describe note <<<"$row"
    [ -n "$note" ] && notes="$notes- $r: not in this run — $note.\n"
  done
  if [ -n "$notes" ]; then printf 'Their own CI keeps these:\n\n'; printf '%b' "$notes"; printf '\n'; fi

  for row in ${rows[@]+"${rows[@]}"}; do
    IFS='|' read -r r status secs describe note <<<"$row"
    [ "$status" = FAIL ] || continue
    printf '<details><summary>%s — last %s log lines</summary>\n\n```\n' "$r" "$log_lines"
    tail -n "$log_lines" "$work/$r.log" | redact
    printf '```\n\n</details>\n\n'
  done

  printf "Don't edit: rewritten on every run. Leave this issue open and pinned.\n"
} > "$body"

if (( dry_run )); then
  echo
  cat "$body"
  echo
  if (( failed )); then echo "dry run: $failed repo(s) would be FAIL" >&2; exit 1; fi
  exit 0
fi

# ---- post --------------------------------------------------------------------
[ -n "${GH_TOKEN:-}" ] || { echo "GH_TOKEN is not set; nothing posted" >&2; exit 1; }
number=$(gh api "repos/$board/issues?state=open&per_page=100" \
  --jq "[.[] | select(.title==\"$issue_title\" and (.pull_request|not))][0].number // empty")
if [ -n "$number" ]; then
  gh api -X PATCH "repos/$board/issues/$number" -F body=@"$body" > /dev/null
  echo "health run: updated #$number"
else
  created=$(gh api "repos/$board/issues" -f title="$issue_title" -F body=@"$body")
  number=$(printf '%s' "$created" | jq -r .number)
  node=$(printf '%s' "$created" | jq -r .node_id)
  echo "health run: opened #$number"
  # Pinning is GraphQL-only. Best effort: an unpinned issue is still readable.
  gh api graphql -f query='mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }' \
    -f id="$node" > /dev/null 2>&1 || echo "could not pin #$number; pin it by hand" >&2
fi

if (( failed )); then echo "health run: $failed repo(s) FAIL" >&2; exit 1; fi
