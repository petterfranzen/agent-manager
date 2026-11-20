"""The "Code Health effect" issue: what finished coding tasks cost, by the Code Health of their files.

Ishikawa's method (section9-hq #348): each coding task's runs in the ledger (tokens, $, timeouts, fix
rounds) joined with the `Code Health: <path> <before> -> <after>` lines that its PRs and their QA
comments carry (agent-manager #130), and bucketed by the "before" score. No CodeScene run: the scores
come from the PRs. GitHub is only read, except for the one issue this writes.
"""

import re
import statistics

from . import board, ledger, net
from .capacity import DASH, share
from .ledger import money
from .when import utc_now

TITLE = "Code Health effect"
CODING = {"task", "fix", "build", "nightly"}    # ledger types that produce code; qa/review/research do not
MAX_TASKS = 200          # the newest coding tasks read per run: ~3 GitHub reads each, far under the rate limit
FIRST_READING = 15       # tasks per arm before the table says anything (the Major, #348)
PUBLIC = 50              # Ishikawa's floor per arm for any public claim (#348, section 4)
BANDS = ((9.5, "high (9.5 and up)"), (7.0, "mid (7 to 9.5)"), (0.0, "low (under 7)"))
NEW, UNSCORED = "new files only", "no scores"
ORDER = [label for _, label in BANDS] + [NEW, UNSCORED]
SCORE = re.compile(r"(?im)^[\s>*-]*Code Health:\s*`?([^\s`]+)`?\s+(new|\d+(?:\.\d+)?)\s*(?:->|→)\s*(\d+(?:\.\d+)?)")
PR_LINK = re.compile(r"https://github\.com/petterfranzen/([\w.-]+)/pull/(\d+)")
OWNER = board.REPO.split("/")[0]
CODE_REPOS = tuple(r for r in board.REPOS if r != "section9-hq")
SHOWN = 20               # tasks listed one by one


def coding_tasks(rows):
    """{issue: its coding runs} for the MAX_TASKS issues whose latest run is newest."""
    by_issue = {}
    for r in rows:
        if r.get("type") in CODING and r.get("issue"):
            by_issue.setdefault(int(r["issue"]), []).append(r)
    newest = sorted(by_issue, key=lambda n: max(r["start"] for r in by_issue[n]), reverse=True)
    return {n: by_issue[n] for n in newest[:MAX_TASKS]}


def texts_of(bd, n, repo=board.REPO):
    """(the issue or PR, its body and every comment's body)."""
    item = bd.call("GET", f"/issues/{n}", repo=repo)
    comments = bd.pages(f"/issues/{n}/comments", repo=repo)
    return item, [item.get("body") or ""] + [c.get("body") or "" for c in comments]


def finished(issue):
    return issue.get("state") == "closed" or "status:done" in board.label_names(issue)


def pr_links(texts):
    """The code PRs the texts link to, in order, each once."""
    found = []
    for text in texts:
        for repo, n in PR_LINK.findall(text):
            if repo in CODE_REPOS and (repo, int(n)) not in found:
                found.append((repo, int(n)))
    return found


def qa_failed(text):
    """A QA comment whose last line is `QA: changes needed`."""
    lines = [line.strip(" *_`") for line in text.splitlines() if line.strip()]
    return bool(lines) and lines[-1].lower() == "qa: changes needed"


def read_scores(texts, files):
    """Add each `Code Health:` line to `files`: the first "before" seen per path, the last "after"."""
    for text in texts:
        for path, before, after in SCORE.findall(text):
            entry = files.setdefault(path, {"before": None if before == "new" else float(before)})
            entry["after"] = float(after)


def evidence(bd, n):
    """{prs, files, qa_fail} for a finished task, or None while it is not finished."""
    issue, texts = texts_of(bd, n)
    if not finished(issue):
        return None
    task = {"prs": pr_links(texts), "files": {}, "qa_fail": False}
    for repo, pr in task["prs"]:
        _, pr_texts = texts_of(bd, pr, f"{OWNER}/{repo}")
        read_scores(pr_texts, task["files"])
        task["qa_fail"] = task["qa_fail"] or any(qa_failed(t) for t in pr_texts[1:])
    return task


def run_tokens(row):
    """Prompt plus completion tokens of one run, or None when the run has no counts."""
    tokens = row.get("tokens") if isinstance(row.get("tokens"), dict) else {}
    if not tokens:
        return None
    return float(tokens.get("prompt_tokens") or 0) + float(tokens.get("completion_tokens") or 0)


def mean_of(files, key):
    scores = [f[key] for f in files.values() if f.get(key) is not None]
    return statistics.mean(scores) if scores else None


def bucket_of(task):
    if not task["files"]:
        return UNSCORED
    if task["before"] is None:
        return NEW
    return next(label for floor, label in BANDS if task["before"] >= floor)


def costed(task, n, rows):
    """The task with its ledger figures and its bucket."""
    counted = [t for t in map(run_tokens, rows) if t is not None]
    task.update(issue=n, runs=len(rows), tokens=sum(counted) if counted else None,
                cost=sum(ledger.run_cost(r) for r in rows), priced=any(ledger.priceable(r) for r in rows),
                timeout=any(r.get("outcome") == "timeout" for r in rows), fix=len(rows) > 1,
                before=mean_of(task["files"], "before"), after=mean_of(task["files"], "after"))
    task["bucket"] = bucket_of(task)
    return task


def gather(ctx, by_issue):
    """(the finished tasks with their evidence, how many could not be read)."""
    tasks, unreadable = [], 0
    for n, rows in by_issue.items():
        try:
            task = evidence(ctx.board, n)
        except net.Failure as e:
            ctx.log(f"code-health: #{n} skipped ({e})")
            unreadable += 1
            continue
        if task is not None:
            tasks.append(costed(task, n, rows))
    return tasks, unreadable


def median(values, fmt):
    return fmt(statistics.median(values)) if values else DASH


def bucket_row(label, tasks):
    n = len(tasks)
    tokens = [t["tokens"] for t in tasks if t["tokens"] is not None]
    costs = [t["cost"] for t in tasks if t["priced"]]
    rates = " | ".join(share(sum(t[k] for t in tasks), n) for k in ("timeout", "fix", "qa_fail"))
    small = " (too few)" if n < FIRST_READING else ""
    return f"| {label}{small} | {n} | {median(tokens, '{:,.0f}'.format)} | {median(costs, money)} | {rates} |"


def bucket_lines(groups):
    lines = ["**By the files' health before the task**",
             "| Before-health | tasks | median tokens | median $ | timeout | fix round | QA fail |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [bucket_row(label, groups.get(label, [])) for label in ORDER]
    return lines + [""]


def reading_lines(groups):
    """Say plainly whether the table can be read yet."""
    after = len(groups.get(BANDS[0][1], []))
    lower = max(len(groups.get(label, [])) for _, label in BANDS[1:])
    if min(after, lower) < FIRST_READING:
        return [f"**Reading: too few tasks to conclude.** The \"after\" arm (before-health 9.5 and up) "
                f"has {after} task(s) and the larger lower bucket {lower}. The first reading is due at "
                f"about {FIRST_READING} in each; until then any difference below is noise.", ""]
    return [f"**Reading: enough for a first look** ({after} tasks at 9.5 and up against {lower}), not "
            f"for a public claim: that needs at least {PUBLIC} per arm (Ishikawa, #348). Task size is not "
            "controlled for here, so compare within one repo before concluding anything.", ""]


def score(value):
    return f"{value:.2f}" if value is not None else "new"


def task_row(t):
    prs = ", ".join(f"{repo}#{n}" for repo, n in t["prs"]) or DASH
    tokens = f"{t['tokens']:,.0f}" if t["tokens"] is not None else DASH
    flags = ", ".join(k for k in ("timeout", "fix", "qa_fail") if t[k]) or DASH
    return (f"| #{t['issue']} | {prs} | {score(t['before'])} | {score(t['after'])} | {tokens} | "
            f"{money(t['cost']) if t['priced'] else DASH} | {t['runs']} | {flags} |")


def task_lines(tasks):
    scored = [t for t in tasks if t["files"]]
    if not scored:
        return ["**Scored tasks:** none yet. A task counts once its PR carries `Code Health:` lines.", ""]
    lines = [f"**Scored tasks, newest first** ({min(len(scored), SHOWN)} of {len(scored)})",
             "| Task | PR | before | after | tokens | $ | runs | flags |", "|---|---|---:|---:|---:|---:|---:|---|"]
    return lines + [task_row(t) for t in scored[:SHOWN]] + [""]


def method_lines(unreadable):
    lines = ["**How it is counted.** Finished (closed or `status:done`) section9-hq tasks of type "
             f"{', '.join(sorted(CODING))}, all ledger history, newest {MAX_TASKS}. Before-health is the "
             "mean \"before\" score over the files the task's PRs list (new files left out; `new files "
             "only` when nothing else). Tokens and $ are summed over the task's runs ($: the key delta, "
             "else its tokens at the model's price). Timeout: any run timed out. Fix round: the task ran "
             "more than once. QA fail: a PR comment ending `QA: changes needed`.", ""]
    if unreadable:
        lines += [f"- {unreadable} task(s) or PR(s) could not be read from GitHub this run and are left out.", ""]
    return lines


def code_health(ctx):
    """Keep one open issue titled "Code Health effect" current."""
    now = utc_now()
    rows = ledger.read_ledger(ledger.ledger_path(ctx.a))
    tasks, unreadable = gather(ctx, coding_tasks(rows))
    groups = {}
    for t in tasks:
        groups.setdefault(t["bucket"], []).append(t)
    lines = [f"**Code Health effect, joined by the VPS from the run ledger and the PRs' `Code Health:` "
             f"lines.** As of {now:%Y-%m-%d %H:%M} UTC. {len(tasks)} finished coding task(s).", ""]
    lines += reading_lines(groups) + bucket_lines(groups) + task_lines(tasks) + method_lines(unreadable)
    lines += ["Rewritten weekly by `vps/dispatch.py code-health`; don't edit. Method and question: #348."]
    board.keep_issue(ctx.board, ctx.log, TITLE, "\n".join(lines))
