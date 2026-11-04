#!/usr/bin/env bash
# One call instead of many: for every open PR in the repos the Major merges in,
# print CI state on the head, the latest QA verdict and whether it came after the
# last push, and whether it is behind its base. The Major reads this table once
# per batch window and merges what is READY. Read-only; REST only (GraphQL is blocked).
set -u
repos="${READY_REPOS:-petterfranzen/flight-tracker petterfranzen/portfolio petterfranzen/ghost-in-the-machine petterfranzen/agent-manager}"
printf '%-34s %-6s %-30s %-8s %-10s %s\n' PR CI QA BEHIND VERDICT TITLE
for repo in $repos; do
  gh api "repos/$repo/pulls?state=open&per_page=30" --jq '.[]|[.number,.head.sha,.base.ref,.user.login,.title,.draft]|@tsv' 2>/dev/null |
  while IFS=$'\t' read -r n sha base user title draft; do
    ci="$(gh api "repos/$repo/commits/$sha/check-runs?per_page=50" --jq '
      [.check_runs[]|select(.name!="deploy")|.conclusion // .status] as $c
      | if ($c|length)==0 then "none"
        elif ($c|any(.=="failure" or .=="cancelled" or .=="timed_out")) then "red"
        elif ($c|any(.=="in_progress" or .=="queued" or .=="pending")) then "wait"
        else "green" end' 2>/dev/null)"
    pushed="$(gh api "repos/$repo/commits/$sha" --jq .commit.committer.date 2>/dev/null)"
    qa="$(gh api "repos/$repo/issues/$n/comments?per_page=100" --jq '
      [.[]|select(.body|test("QA: (pass|changes needed)"))] | last
      | if .==null then "none|" else (.created_at + "|" + (.body|capture("QA: (?<v>pass|changes needed)")|.v)) end' 2>/dev/null)"
    qat="${qa%%|*}"; qv="${qa##*|}"
    state="no QA"
    if [ "$qv" = "pass" ]; then
      if [[ "$qat" > "$pushed" ]]; then state="QA pass"; else state="QA stale"; fi
    elif [ "$qv" = "changes needed" ]; then state="QA changes"; fi
    behind="$(gh api "repos/$repo/compare/$base...$sha" --jq .behind_by 2>/dev/null)"
    verdict="-"
    if [ "$user" = "dependabot[bot]" ]; then verdict="24h-rule"
    elif [ "$ci" = green ] && [ "$state" = "QA pass" ] && [ "${draft:-false}" = false ]; then
      verdict="READY"; [ "${behind:-0}" != 0 ] && verdict="UPDATE"
    fi
    printf '%-34s %-6s %-30s %-8s %-10s %s\n' "${repo#petterfranzen/}#$n" "$ci" "$state" "${behind:-?}" "$verdict" "${title:0:60}"
  done
done
