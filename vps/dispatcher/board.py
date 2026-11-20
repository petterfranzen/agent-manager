"""The task board: issues in section9-hq, their labels, and which agents may take one."""

import re
import urllib.parse

from . import net, when

REPO = "petterfranzen/section9-hq"
PER_PAGE, MAX_PAGES = 100, 20     # board reads follow Link: rel="next" up to this; more than that is refused
PRIORITY_AGING_HOURS = 12.0      # a task waiting this long moves up one priority level (#100)
LOW_PRIORITY_AGING_HOURS = 48.0  # a priority:low task waiting this long runs ahead of high work (#100)
# The repos a task may name in its title or body when it carries no `repo:<name>` label.
# Anything else is unknown, and an unknown task only ever runs in lane 0.
REPOS = ("agent-manager", "section9-hq", "ghost-in-the-machine", "flight-tracker",
         "nvim-trainer", "dinner-planner", "portfolio")
ANY = "any"
AUTHOR = re.compile(r"(?im)^\s*author:\s*([A-Za-z][\w.-]*)")
# The issues the VPS keeps current, by title: (the word its log lines start with, pin a new one).
KEPT_ISSUES = {"Spend": ("spend", True), "Capacity": ("capacity", True), "CPU credits": ("cpu", False),
               "Code Health effect": ("code-health", False)}
PIN_QUERY = "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }"


def issues_only(items):
    """GitHub lists pull requests among the issues; drop them."""
    return [i for i in items if "pull_request" not in i]


class Board:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/"), token

    def call(self, method, path, body=None, repo=REPO):
        """One REST call on section9-hq, or on `repo` (another of Petter's repos, read-only by convention)."""
        return net.http(method, f"{self.api}/repos/{repo}{path}", self.token, body=body)

    def pages(self, path, repo=REPO):
        """Every item of a list endpoint, following Link: rel="next". Refuses to go on past MAX_PAGES pages
        (a silently short list would hide the oldest todos), or to send the token to another host."""
        url = f"{self.api}/repos/{repo}{path}{'&' if '?' in path else '?'}per_page={PER_PAGE}"
        items = []
        for _ in range(MAX_PAGES):
            page, headers = net.request("GET", url, self.token)
            items += page or []
            url = net.next_link(headers)
            if not url:
                return items
            if urllib.parse.urlsplit(url)[:2] != urllib.parse.urlsplit(self.api)[:2]:
                raise net.Failure(f"GET {path} -> the next page is on another host; refusing to send the token there")
        raise net.Failure(f"GET {path} -> more than {MAX_PAGES * PER_PAGE} items; refusing to work from a partial list")

    def todo(self):
        """Open status:todo issues (not PRs), oldest first."""
        return issues_only(self.pages("/issues?labels=status%3Atodo&state=open&sort=created&direction=asc"))

    def working(self):
        """Open status:working issues (not PRs)."""
        return issues_only(self.pages("/issues?labels=status%3Aworking&state=open"))

    def comment(self, n, text):
        self.call("POST", f"/issues/{n}/comments", {"body": net.redact(text)[:60000]})

    def set_status(self, issue, status, agent=None):
        labels = [l["name"] for l in issue["labels"] if not l["name"].startswith("status:")]
        if agent is not None:      # a pooled task becomes that agent's own, so a killed run is swept with its timeout
            labels = [x for x in labels if not x.lower().startswith("agent:")] + ["agent:" + agent["name"].lower()]
        self.call("PATCH", f"/issues/{issue['number']}", {"labels": labels + ["status:" + status]})

    def block(self, issue, why):
        """Set `status:blocked` and say why in a comment."""
        self.set_status(issue, "blocked")
        self.comment(issue["number"], why)

    def open_titled(self, title):
        """The oldest open issue (not a PR) with exactly this title, or None."""
        open_ = self.pages("/issues?state=open&sort=created&direction=asc")
        return next((i for i in issues_only(open_) if i["title"] == title), None)

    def pin(self, issue):
        """Pinning is GraphQL-only and needs more than issue access: the caller treats it as best effort."""
        net.http("POST", f"{self.api}/graphql", self.token,
                 body={"query": PIN_QUERY, "variables": {"id": issue["node_id"]}})


def keep_issue(board, log, title, body):
    """Keep the one open issue with this title current: rewrite its body, or open (and pin) it."""
    tag, pinned = KEPT_ISSUES[title]
    issue = board.open_titled(title)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": body})
        log(f"{tag}: updated #{issue['number']}")
        return
    issue = board.call("POST", "/issues", {"title": title, "body": body})
    log(f"{tag}: opened #{issue['number']}")
    if not pinned:
        return
    try:
        board.pin(issue)
    except (net.Failure, KeyError) as e:
        log(f"{tag}: could not pin #{issue['number']} (pin it by hand): {e}")


def label_names(issue):
    return {l["name"].lower() for l in issue["labels"]}


def labelled(issue, agents):
    """The agents whose own `agent:<name>` label the issue carries, in roster order."""
    names = label_names(issue)
    return [a for a in agents if f"agent:{a['name'].lower()}" in names]


def agent_of(issue, agents):
    return next(iter(labelled(issue, agents)), None)


def label_value(issue, prefix):
    """What follows `prefix` in the first label (sorted) that has it and says something, else None."""
    values = (name[len(prefix):].strip() for name in sorted(label_names(issue)) if name.startswith(prefix))
    return next((v for v in values if v), None)


def author_of(issue):
    """Who wrote the thing under review: an `author:<name>` label, else an `Author: <Name>` line in the body."""
    label = label_value(issue, "author:")
    if label:
        return label.lower()
    m = AUTHOR.search(issue.get("body") or "")
    return m.group(1).strip().lower() if m else None


def role_matches(agent, token):
    """`agent:<role>`: a word in the agent's role or title. `agent:qa` reaches Togusa."""
    words = ((agent.get("role") or "") + " " + (agent.get("title") or "")).lower().split()
    return token in words


def pool_token(issue):
    """The `any` or role of an issue's first `agent:` label, or None.

    sorted: an issue with two agent labels (e.g. agent:any and agent:qa) must choose the same
    one every run; set order depends on PYTHONHASHSEED. 'any' sorts before a role, so it wins a tie.
    """
    return next((n.split(":", 1)[1].strip() for n in sorted(label_names(issue)) if n.startswith("agent:")), None)


def pool_of(token, agents, author):
    """(the agents an `agent:any` or `agent:<role>` label reaches, minus the author; why none)."""
    pool = list(agents) if token == ANY else [a for a in agents if role_matches(a, token)]
    if not pool:
        return [], f"no agent has the role {token!r}"
    pool = [a for a in pool if a["name"].lower() != author]
    if not pool:
        return [], f"the author ({author}) is the only agent who could take it"
    return pool, ""


def candidates(issue, agents):
    """(agents who may take it, why not). An exact `agent:<name>` wins; `agent:any` or `agent:<role>`
    is a pool. Either way the author of the thing under review is never among them."""
    author = author_of(issue)
    named = agent_of(issue, agents)
    if named and named["name"].lower() == author:
        return [], f"the named agent, {named['name']}, wrote it"
    if named:
        return [named], ""
    token = pool_token(issue)
    if not token:
        return [], "no agent label"
    return pool_of(token, agents, author)


def activity(board, agents):
    """Agent name -> its most recent `updated_at` on the board, from one page of the 100 most
    recently updated issues. An agent outside that window is treated as idle longest."""
    seen = {}
    recent = board.call("GET", "/issues?state=all&sort=updated&direction=desc&per_page=100") or []
    for issue in issues_only(recent):
        at = issue.get("updated_at") or ""
        for a in labelled(issue, agents):
            seen[a["name"]] = max(at, seen.get(a["name"], at))
    return {name: at for name, at in seen.items() if at}


def repo_key(issue):
    """The repo a task touches: its `repo:<name>` label, else a known repo named in the title or body.
    '' means unknown, and an unknown task only ever runs in lane 0 (#79)."""
    label = label_value(issue, "repo:")
    if label:
        return label
    text = ((issue.get("title") or "") + "\n" + (issue.get("body") or "")).lower()
    return next((r for r in REPOS if r in text), "")


def inflight(board, agents):
    """(agents, repos, unknown) held by the open `status:working` issues: what the other lane is on.

    Read from the board, not from a state file, so a killed lane leaves nothing behind. `unknown`
    is True when a working task names no repo we recognise: then a second lane could collide with
    it, so it stands down (a `repo:<name>` label lets it run)."""
    working = board.working()
    holders = [agent_of(issue, agents) for issue in working]
    keys = [repo_key(issue) for issue in working]
    names = {agent["name"].lower() for agent in holders if agent}
    return names, {key for key in keys if key}, not all(keys)


def has_priority(issue, level):
    """True when the issue carries `priority:<level>`. Goes through label_names, which lowercases, so
    the case of the label on GitHub never matters; the pick and the pacing rule both use this."""
    return f"priority:{level}" in label_names(issue)


def age_hours(issue, now=None):
    """How long the issue has waited, in hours from its `created_at`; None when that cannot be read."""
    at = when.parse_when(issue.get("created_at"))
    if at is None:
        return None
    now = now or when.utc_now()
    return max((now - at).total_seconds() / 3600.0, 0.0)


def label_rank(issue):
    """`priority:high` 0, unlabelled (or anything else) 1, `priority:low` 2."""
    if has_priority(issue, "high"):
        return 0
    return 2 if has_priority(issue, "low") else 1


def priority_rank(issue, now=None):
    """Pick order: `priority:high` (0), then unlabelled (1), then `priority:low` (2), oldest first
    within each group. Anything else ranks 1.

    Petter, 4 Nov: a bug must not wait behind older feature requests. Aging (Petter, 5 Nov, #100): a
    stream of `priority:high` tasks must not starve the rest for ever. A task that has waited
    `PRIORITY_AGING_HOURS` moves up one level, and a `priority:low` task that has waited
    `LOW_PRIORITY_AGING_HOURS` moves to the top (0), so it runs even while high tasks exist. With no
    priority labels and young tasks every issue ranks 1, so the pick is oldest-first exactly as before.
    """
    rank = label_rank(issue)
    age = age_hours(issue, now)
    if age is None:
        return rank
    if has_priority(issue, "low") and age >= LOW_PRIORITY_AGING_HOURS:
        return 0
    if age >= PRIORITY_AGING_HOURS:
        rank = max(rank - 1, 0)
    return rank
