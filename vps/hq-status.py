#!/usr/bin/env python3
"""HQ page files for hq.sectionnine.dev, written into the directory the hq container serves.

  hq-status.py status   write status.json and board/board.json (every 2 min): what each agent is
                        doing, from the issues in section9-hq, plus the month's OpenRouter usage.
                        One run also installs the waits board from vps/board/ as board/index.html
  hq-status.py crew     fetch main's crew/ folder from ghost-in-the-machine (every 10 min),
                        and its hq/*.yaml dashboard themes into --themes-dir

Two copies of the document. board/board.json is what the waits board polls; the board is
served only behind the Cloudflare Access app that admits Petter alone, so it carries the real
task titles, issue and PR links and "Waits on" notes (Petter, 12 Nov: a board of "On a task"
cards tells him nothing). The site-root status.json is kept for a later public crew page and
stays allowlisted: an issue labelled `private`, or whose title matches a term in
<secrets-dir>/crew-deny.txt (one per line), shows as "On a private task", and without that file
only agent states and "On a task" are published. Only the board copy carries money
amounts (month, cap, today, per-agent cap and last week's spend); the root copy has a
percentage only. status.json keeps the format of crew/status.json in ghost-in-the-machine,
plus a `model` field. Standard library only. Secrets (never printed):
  GH_TOKEN             in <secrets-dir>/sandbox.env
  OPENROUTER_API_KEY   in <secrets-dir>/hermes.env
"""

import argparse
import base64
import datetime as dt
import json
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BOARD = "petterfranzen/section9-hq"
CREW_REPO = "petterfranzen/ghost-in-the-machine"
OWNER_URLS = ("https://github.com/petterfranzen/", "https://petterfranzen.github.io/")   # board only
PUBLIC_URLS = (f"https://github.com/{CREW_REPO}/pull/", "https://petterfranzen.github.io/ghost-in-the-machine/")
URL = re.compile(r"https://(?:github\.com/petterfranzen/ghost-in-the-machine/pull/\d+|petterfranzen\.github\.io/ghost-in-the-machine/[\w./-]*)")
KEYS = re.compile(r"(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DENY_BASE = r"petter|franz[eé]n"     # the owner's name never shows, whatever the deny file says
PRIVATE, ON_TASK = "On a private task", "On a task"
WAITS = ("petter", "major", "qa", "agent", "task")    # board.json `waits` column, in page order
WAITS_ON = re.compile(r"^[ \t]*waits\s+on\b[^\n]*", re.IGNORECASE | re.MULTILINE)
# Each card's number links to its issue; only a non-private task gets one.
ISSUE = f"https://github.com/{BOARD}/issues/"
RECENT, TEXT_MAX = 5, 90
ZULU = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_FILE, MAX_TOTAL = 5 << 20, 50 << 20
THEME_FILE, MAX_THEME = re.compile(r"^[A-Za-z0-9][\w.-]*\.yaml$"), 64 << 10
THEME_NAME = re.compile(r"^name:\s*\S", re.MULTILINE)


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def unquoted(val):
    """A value without the one pair of matching quotes around it."""
    val = val.strip()
    paired = len(val) >= 2 and val[0] == val[-1]
    if paired and val[0] in "\"'":
        return val[1:-1]
    return val


def env_pair(line):
    """(name, value) of one NAME=VALUE line of an env file; None for a comment or anything else."""
    line = line.strip().removeprefix("export ").lstrip()
    key, sep, val = line.partition("=")
    if not sep or line.startswith("#"):
        return None
    return key.strip(), unquoted(val)


def read_env_value(path, name):
    """The last NAME=VALUE line for `name` in an env file. Missing or empty is a Failure."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    pairs = [env_pair(line) for line in text.splitlines()]
    values = [pair[1] for pair in pairs if pair and pair[0] == name]
    value = values[-1] if values else None
    if not value:
        raise Failure(f"{name} is missing or empty in {path}")
    return value


def http(url, token=None):
    req = urllib.request.Request(url)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "section9-hq-status")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        raise Failure(f"GET {redact(url)} -> {e.code}: {redact(e.read().decode('utf-8', 'replace'))[:200]}") from None
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise Failure(f"GET {redact(url)} -> {redact(str(getattr(e, 'reason', e)))}") from None


def iso(value):
    if isinstance(value, str):
        value = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def short(text):
    text = " ".join(str(text).split())
    return text if len(text) <= TEXT_MAX else text[:TEXT_MAX - 1].rstrip() + "…"


def model_of(config_text):
    m = re.search(r"^model:\s*\n(?:[ \t]+.*\n|\s*\n)*?[ \t]+default:\s*([^\s#]+)", config_text, re.MULTILINE)
    return m.group(1) if m else None


def usd(value):
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


def week_table(body):
    """The lines of the table under the first "Last 7 days by agent" heading."""
    lines = [line.strip() for line in (body or "").splitlines()]
    start = next((n for n, line in enumerate(lines) if "last 7 days by agent" in line.lower()), len(lines))
    rows = []
    for line in lines[start + 1:]:
        if line.startswith("|"):
            rows.append(line)
        elif rows or line.startswith("#"):
            break
    return rows


def week_row(line):
    """(lower-case name, week) of one table row; None for the header and the --- row."""
    cells = [c.strip() for c in line.strip("|").split("|")]
    nums = [re.sub(r"[$,\s]", "", c) for c in cells[1:6]]
    try:
        runs, busy, spend, _, timeouts = (float(n) for n in nums)
    except ValueError:
        return None
    return re.sub(r"[*`]", "", cells[0]).strip().lower(), {
        "runs": int(runs), "busyH": round(busy, 1), "spendUsd": round(spend, 2), "timeouts": int(timeouts)}


def capacity(body):
    """The "Last 7 days by agent" table in the body of section9-hq #80 ("Capacity"), rows
    `| Name | runs | busy h | $ | $/h | timeouts | timeout h | credits |`. Returns
    ({lower-case name: week}, the "As of ... UTC" time as ISO or None)."""
    rows = [week_row(line) for line in week_table(body)]
    weeks = dict(row for row in rows if row)
    m = re.search(r"As of\s+(\d{4}-\d\d-\d\d)[ T](\d\d:\d\d)\s*UTC", body or "")
    return weeks, (m.group(1) + "T" + m.group(2) + ":00Z" if m else None)


def labels(issue):
    return {l["name"] for l in issue.get("labels", [])}


def label_values(issue, prefix):
    """What follows `prefix` in each of the issue's labels that has it."""
    return [l[len(prefix):] for l in labels(issue) if l.startswith(prefix)]


def issue_key(comment):
    """The number of the issue a comment is on, as text."""
    return comment["issue_url"].rsplit("/", 1)[-1]


def latest_match(comments, pattern):
    """Issue number -> the pattern's match in that issue's latest comment that has one.
    Comments come oldest first, so a later comment wins."""
    found = {}
    for c in comments:
        m = pattern.search(c.get("body") or "")
        if m:
            found[issue_key(c)] = m.group(0)
    return found


def status_of(issue):
    """The board column: done for a closed issue, else its first status label by weight."""
    names = labels(issue)
    if "status:done" in names or issue.get("state") == "closed":
        return "done"
    return next((s for s in ("blocked", "review", "working", "todo") if f"status:{s}" in names), "todo")


class Copy:
    """One copy of the status document, and how much it may show.

    `reveal` is the board's own copy (board/board.json): the board is served only behind the
    Cloudflare Access app that admits Petter alone (RUNBOOK), so it shows real titles, issue links,
    PR links in any of Petter's repos and the "Waits on" notes. The site-root status.json, kept for a
    later public crew page, is always built with reveal=False and stays redacted. `deny` is a
    compiled regex, or None when there is no deny file."""

    def __init__(self, deny, comments, reveal=False):
        self.reveal = reveal
        # On the board, a never-matching pattern: "a deny list exists, it is empty".
        self.deny = (deny or re.compile(r"(?!)")) if reveal else deny
        self.base = re.compile(DENY_BASE, re.IGNORECASE)
        self.pr_ok = OWNER_URLS if reveal else PUBLIC_URLS
        self.links = latest_match(comments, URL)
        self.waits_text = {n: short(text.strip()) for n, text in latest_match(comments, WAITS_ON).items()}

    def is_private(self, issue, title):
        if "private" in labels(issue) or self.base.search(title):
            return True
        return bool(self.deny and self.deny.search(title))

    def text(self, issue):
        """The title as this copy may show it."""
        title = issue.get("title") or ""
        if self.reveal:
            return short(title)
        if self.is_private(issue, title):
            return PRIVATE
        return short(title) if self.deny else ON_TASK

    def link(self, issue):
        """The PR or page a task points at: its latest comment's link, else the one in its body."""
        url = URL.search(issue.get("body") or "")
        return self.links.get(str(issue["number"])) or (url.group(0) if url else None)

    def recent_item(self, issue):
        item = {"at": iso(issue.get("closed_at") or issue["updated_at"]), "text": self.text(issue)}
        url = self.link(issue)
        if item["text"] != PRIVATE and self.may_link(url):
            item["url"] = url
        return item

    def may_link(self, url):
        """True for a link a finished task may show: any on the board, only the public ones elsewhere."""
        if not url:
            return False
        return self.reveal or url.startswith(PUBLIC_URLS)

    def recent(self, newest):
        """An agent's finished work, newest first; a run of private tasks shows as one."""
        if not self.deny:
            return []
        items = []
        for issue in newest:
            if not labels(issue) & {"status:done", "status:review"}:
                continue
            item = self.recent_item(issue)
            if not (items and item["text"] == PRIVATE == items[-1]["text"]):
                items.append(item)
        return items

    def now_text(self, working):
        if not working:
            return None
        return self.text(working[0]) if self.deny else ON_TASK

    def agent(self, r, issues):
        mine = [i for i in issues if f"agent:{r['name'].lower()}" in labels(i) and "pull_request" not in i]
        newest = sorted(mine, key=lambda i: i["updated_at"], reverse=True)
        working = [i for i in newest if "status:working" in labels(i)]
        return {
            "name": r["name"],
            "role": r.get("title") or r.get("role") or "",
            "state": "working" if working else "idle",
            "now": self.now_text(working),
            "lastActiveAt": iso(newest[0]["updated_at"]) if newest else None,
            "budgetPercent": 0,       # per-agent spend isn't tracked on the VPS
            "recent": self.recent(newest)[:RECENT],
        }

    def pr(self, issue):
        url = self.link(issue)
        return url if url and url.startswith(self.pr_ok) else None

    def task(self, issue):
        title = self.text(issue)
        private = title == PRIVATE
        number = issue["number"]
        return {
            "id": number,
            "title": title,
            "agent": next(iter(label_values(issue, "agent:")), "unassigned"),
            "status": status_of(issue),
            # the waits:* label picks the page column
            "waits": next((w for w in label_values(issue, "waits:") if w in WAITS), "agent"),
            "waitsText": "" if private else self.waits_text.get(str(number), ""),
            "url": None if private else f"{ISSUE}{number}",
            "pr": None if private else self.pr(issue),
            "updated": iso(issue["updated_at"]),
        }


def board_fields(r, weeks):
    """What only the board's copy says about an agent: its model, caps and last week."""
    rpd, cents = r.get("runs_per_day"), r.get("budget_monthly_cents")
    runs_ok = isinstance(rpd, int) and rpd >= 0
    return {
        "model": str(r["model"]).split("/", 1)[-1] if r.get("model") else "DeepSeek (default)",
        "runsPerDay": rpd if runs_ok else None,
        "budgetMonthlyUsd": usd(cents / 100) if isinstance(cents, (int, float)) else None,
        "week": (weeks or {}).get(r["name"].lower()),
    }


def used_percent(key):
    used, cap = key.get("usage_monthly"), key.get("limit")
    if used is None or not cap:
        return 0
    return round(100 * float(used) / float(cap))


def build(src, deny, board=None):
    """The status document from `src`: now, agents, issues, comments, key (OpenRouter's) and model.

    `board` is None for the redacted site-root copy, or (weeks, as_of) from capacity() for the
    board's own copy. Only the board copy carries dollar amounts (budget, per-agent cap and last
    week's spend)."""
    reveal = board is not None
    copy = Copy(deny, src["comments"], reveal)
    key, now = src["key"], src["now"]
    agents = [copy.agent(r, src["issues"]) for r in src["agents"]]
    doc = {
        "generatedAt": iso(now),
        "budget": {"usedPercent": used_percent(key), "month": now.strftime("%Y-%m")},
        "model": src["model"],
        "agents": agents,
        "tasks": [copy.task(i) for i in src["issues"] if "pull_request" not in i],
    }
    if reveal:
        weeks, as_of = board
        for entry, r in zip(agents, src["agents"]):
            entry.update(board_fields(r, weeks))
        doc["budget"].update({"usedUsd": usd(key.get("usage_monthly")), "capUsd": usd(key.get("limit")),
                              "leftUsd": usd(key.get("limit_remaining")), "todayUsd": usd(key.get("usage_daily"))})
        doc["capacityAsOf"] = as_of
    validate(doc, copy.deny, reveal)
    return doc


MONEY = ("usedUsd", "capUsd", "leftUsd", "todayUsd")
AGENT_KEYS = {"name", "role", "state", "now", "lastActiveAt", "budgetPercent", "recent"}
BOARD_AGENT_KEYS = {"model", "runsPerDay", "budgetMonthlyUsd", "week"}


def need(ok, what):
    if not ok:
        raise Failure(f"status.json invalid: {what}")


def allowed_urls(reveal):
    return OWNER_URLS if reveal else PUBLIC_URLS


def validate_top(doc, reveal):
    top = {"generatedAt", "budget", "model", "agents", "tasks"} | ({"capacityAsOf"} if reveal else set())
    need(set(doc) == top and ZULU.match(doc["generatedAt"]), "top level")
    need(isinstance(doc["budget"]["usedPercent"], int) and doc["budget"]["usedPercent"] >= 0, "budget")
    need(set(doc["budget"]) == {"usedPercent", "month"} | (set(MONEY) if reveal else set()), "budget keys")
    if reveal:
        need(all(doc["budget"][k] is None or isinstance(doc["budget"][k], float) for k in MONEY), "budget amounts")
        need(doc["capacityAsOf"] is None or ZULU.match(doc["capacityAsOf"]), "capacityAsOf")


def validate_board_agent(a):
    need(isinstance(a["model"], str) and (a["runsPerDay"] is None or isinstance(a["runsPerDay"], int)), "agent model/runs")
    need(a["budgetMonthlyUsd"] is None or isinstance(a["budgetMonthlyUsd"], float), "agent budget")
    need(a["week"] is None or set(a["week"]) == {"runs", "busyH", "spendUsd", "timeouts"}, "agent week")


def validate_agent(a, reveal):
    need(a["state"] in ("working", "idle") and (a["lastActiveAt"] is None or ZULU.match(a["lastActiveAt"])), "agent")
    need(set(a) == AGENT_KEYS | (BOARD_AGENT_KEYS if reveal else set()), "agent keys")
    if reveal:
        validate_board_agent(a)
    for r in a["recent"]:
        need(ZULU.match(r["at"]) and ("url" not in r or r["url"].startswith(allowed_urls(reveal))), "recent item")


def validate_task(t, reveal):
    need(isinstance(t.get("id"), int), "task id")
    need(isinstance(t.get("title"), str), "task title")
    need(isinstance(t.get("agent"), str), "task agent")
    need(t.get("status") in ("todo", "working", "review", "blocked", "done"), "task status")
    need(t.get("waits") in WAITS, "task waits")
    need(isinstance(t.get("waitsText"), str), "task waitsText")
    need(t.get("url") is None or (isinstance(t["url"], str) and t["url"].startswith(ISSUE)), "task url")
    need(t.get("pr") is None or (isinstance(t["pr"], str) and t["pr"].startswith(allowed_urls(reveal))), "task pr")
    need(isinstance(t.get("updated"), str) and ZULU.match(t["updated"]), "task updated")


def shown_words(doc):
    """Every piece of free text the document shows."""
    agents, tasks = doc["agents"], doc["tasks"]
    return [a["name"] for a in agents] + [a["now"] or "" for a in agents] \
        + [r["text"] for a in agents for r in a["recent"]] \
        + [t["title"] for t in tasks] + [t["waitsText"] for t in tasks]


def validate_public(doc, deny):
    """The root copy may go public: no private term and no money amount in it."""
    words = shown_words(doc)
    pattern = re.compile(DENY_BASE + ("|" + deny.pattern if deny else ""), re.IGNORECASE)
    need(not any(pattern.search(w) for w in words), "a private term would be published")
    need(not re.search(r"\$\s?\d|cents|usd", json.dumps(doc), re.IGNORECASE), "a money amount would be published")


def validate(doc, deny, reveal=False):
    validate_top(doc, reveal)
    for a in doc["agents"]:
        validate_agent(a, reveal)
    for t in doc["tasks"]:
        validate_task(t, reveal)
    if not reveal:      # the board sits behind Access (Petter only) and is the only copy with dollar amounts
        validate_public(doc, deny)


def write_atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def load_deny(secrets_dir):
    try:
        terms = [t.strip() for t in (Path(secrets_dir) / "crew-deny.txt").read_text().splitlines()
                 if t.strip() and not t.startswith("#")]
    except OSError:
        return None
    return re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE) if terms else None


def first_readable(paths):
    """The bytes of the first file that can be read, or None."""
    for path in paths:
        try:
            return path.read_bytes()
        except OSError:
            continue
    return None


def board_assets(repo_root):
    """The page to serve, from the vendored vps/board/. A change there applies through the
    sync like any other (CI green, soak). When the real page is missing, the old placeholder is
    kept as the fallback, so /board still renders. Returns (page bytes, sample bytes or None)."""
    board = Path(repo_root) / "vps" / "board"
    page = first_readable([board / "index.html", board / "placeholder.html"])
    if page is None:
        raise Failure("no board page: vps/board/index.html and placeholder.html are both missing")
    return page, first_readable([board / "board-sample.json"])


def read_keys(a):
    """(GitHub token, OpenRouter key). Refuses to send either over plain HTTP to a remote host."""
    ghtoken = read_env_value(Path(a.secrets_dir) / "sandbox.env", "GH_TOKEN")
    orkey = read_env_value(Path(a.secrets_dir) / "hermes.env", "OPENROUTER_API_KEY")
    secrets.extend([ghtoken, orkey])
    for url in (a.github, a.openrouter):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" and u.hostname not in LOOPBACK:
            raise Failure(f"refusing to send a key over plain HTTP to {u.hostname}")
    return ghtoken, orkey


def read_repo(repo_root):
    """(the roster's agents, the default model in the Hermes config)."""
    try:
        agents = tomllib.loads((Path(repo_root) / "agents" / "roster.toml").read_text(encoding="utf-8"))["agents"]
        model = model_of((Path(repo_root) / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        raise Failure(f"cannot read the roster or Hermes config: {e}") from None
    return agents, model


def week_figures(board, ghtoken):
    """capacity() of the "Capacity" issue; without it the board still builds, minus the week figures."""
    try:
        return capacity((http(f"{board}/issues/80", ghtoken) or {}).get("body"))
    except (Failure, AttributeError):
        return {}, None


def as_json(doc):
    return (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode()


def do_status(a):
    ghtoken, orkey = read_keys(a)
    agents, model = read_repo(a.repo_root)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    board = f"{a.github.rstrip('/')}/repos/{BOARD}"
    issues = http(f"{board}/issues?state=all&sort=updated&direction=desc&per_page=100", ghtoken) or []
    since = iso(now - dt.timedelta(days=14))
    comments = http(f"{board}/issues/comments?since={since}&per_page=100", ghtoken) or []
    key = (http(a.openrouter.rstrip("/") + "/key", orkey) or {}).get("data") or {}
    src = {"now": now, "agents": agents, "issues": issues, "comments": comments, "key": key, "model": model}
    weeks = week_figures(board, ghtoken)
    deny = load_deny(a.secrets_dir)
    doc = build(src, deny)
    write_atomic(Path(a.site_dir) / "status.json", as_json(doc))            # redacted: may be public one day
    board_data = as_json(build(src, deny, board=weeks))
    page, sample = board_assets(a.repo_root)
    board_dir = Path(a.site_dir) / "board"
    write_atomic(board_dir / "board.json", board_data)    # what the waits page polls: behind Access, real titles
    write_atomic(board_dir / "status.json", board_data)   # kept: the placeholder page reads this
    if sample is not None:
        write_atomic(board_dir / "board-sample.json", sample)     # the page's own fallback data
    write_atomic(board_dir / "index.html", page)
    print(f"hq status: {len(doc['agents'])} agents, {sum(x['state'] == 'working' for x in doc['agents'])} working, {len(doc['tasks'])} tasks")


def unchanged(state, sha, served):
    """True when the tree was already mirrored (the state file holds its sha) and `served` is there."""
    try:
        return state.read_text().strip() == sha and served.exists()
    except OSError:
        return False


def mirrored(entry):
    """True for a crew/ entry to mirror: a file inside the folder, never the live status.json and
    never anything under board/, which section9-hq-status alone writes (the board is served
    from its own origin, so crew/ must not be able to add pages to it)."""
    path = entry["path"]
    if entry["type"] != "blob" or path == "status.json":
        return False
    if path == "board" or path.startswith("board/"):
        return False
    return ".." not in path.split("/") and not path.startswith("/")


def crew_files(tree):
    """path -> blob sha of what to mirror. Refuses a file or a total that is too large."""
    files, total = {}, 0
    for e in filter(mirrored, tree["tree"]):
        total += e.get("size", 0)
        if e.get("size", 0) > MAX_FILE or total > MAX_TOTAL:
            raise Failure(f"crew/{e['path']} is too large")
        files[e["path"]] = e["sha"]
    return files


def kept(rel):
    """Paths in the site dir that crew/ does not own: the live status.json, the board, half-written files."""
    if rel == "status.json" or rel.endswith(".tmp"):
        return True
    return rel == "board" or rel.startswith("board/")


def prune(site, files):
    """Remove the files and folders no longer in crew/."""
    for p in sorted(site.rglob("*"), reverse=True):
        rel = p.relative_to(site).as_posix()
        if kept(rel):
            continue
        if p.is_file() and rel not in files:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()


def do_crew(a):
    """Mirror main's crew/ into the site dir (never its status.json, which is live)."""
    api = f"{a.github.rstrip('/')}/repos/{CREW_REPO}/git"
    tree = http(f"{api}/trees/main%3Acrew?recursive=1")
    if tree.get("truncated"):
        raise Failure("crew/ listing is truncated")
    site, state = Path(a.site_dir), Path(a.state_dir) / "hq-crew.sha"
    if unchanged(state, tree["sha"], site / "index.html"):
        print("hq crew: unchanged")
        return
    files = crew_files(tree)
    if "index.html" not in files:
        raise Failure("crew/index.html is missing; keeping what is served")
    for rel, sha in files.items():
        blob = http(f"{api}/blobs/{sha}")
        write_atomic(site / rel, base64.b64decode(blob["content"]))
    prune(site, files)
    write_atomic(state, tree["sha"].encode())
    print(f"hq crew: {len(files)} files")


def themes_tree(api):
    """The listing of main's hq/ folder; an empty one when the folder does not exist yet."""
    try:
        tree = http(f"{api}/trees/main%3Ahq")
    except Failure as e:
        if "-> 404" not in str(e):
            raise
        tree = {"sha": "none", "tree": []}
    if tree.get("truncated"):
        raise Failure("hq/ listing is truncated")
    return tree


def theme(api, entry):
    """One theme file's bytes. Refuses one that is too large or has no `name:`."""
    if entry.get("size", 0) > MAX_THEME:
        raise Failure(f"hq/{entry['path']} is too large")
    data = base64.b64decode(http(f"{api}/blobs/{entry['sha']}")["content"])
    if not THEME_NAME.search(data.decode("utf-8", "replace")):
        raise Failure(f"hq/{entry['path']} has no `name:`; keeping what is served")
    return data


def theme_files(api, tree):
    """name -> bytes of every hq/*.yaml in the listing."""
    files = {}
    for e in tree["tree"]:
        if e["type"] == "blob" and THEME_FILE.match(e["path"]):
            files[e["path"]] = theme(api, e)
    return files


def prune_themes(out, files):
    """Remove the themes no longer in hq/ (never a half-written file)."""
    for p in out.glob("*"):
        stale = p.is_file() and p.name not in files
        if stale and not p.name.endswith(".tmp"):
            p.unlink()


def starter_theme(repo_root):
    try:
        return (Path(repo_root) / "vps" / "dashboard-themes" / "ghost.yaml").read_bytes()
    except OSError as e:
        raise Failure(f"no ghost theme and no starter: {e}") from None


def do_themes(a):
    """Mirror main's hq/*.yaml (Hermes dashboard themes); ghost falls back to the starter in this repo."""
    api = f"{a.github.rstrip('/')}/repos/{CREW_REPO}/git"
    tree = themes_tree(api)
    out, state = Path(a.themes_dir), Path(a.state_dir) / "hq-themes.sha"
    if unchanged(state, tree["sha"], out / "ghost.yaml"):
        print("hq themes: unchanged")
        return
    files = theme_files(api, tree)
    if "ghost.yaml" not in files:
        files["ghost.yaml"] = starter_theme(a.repo_root)
    for rel, data in files.items():
        write_atomic(out / rel, data)
    prune_themes(out, files)
    write_atomic(state, tree["sha"].encode())
    print(f"hq themes: {len(files)} files")


def do_crew_and_themes(a):
    """Two independent jobs on one timer: a failure in one doesn't stop the other."""
    errors = []
    for job in (do_crew, do_themes):
        try:
            job(a)
        except Failure as e:
            errors.append(str(e))
    if errors:
        raise Failure("; ".join(errors))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("cmd", choices=["status", "crew"])
    p.add_argument("--secrets-dir", default="/etc/section9")
    p.add_argument("--state-dir", default="/var/lib/section9")
    p.add_argument("--site-dir", default="/var/lib/section9/hq")
    p.add_argument("--themes-dir", default="/var/lib/section9/dashboard-themes")
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--github", default="https://api.github.com")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    a = p.parse_args(argv)
    try:
        {"status": do_status, "crew": do_crew_and_themes}[a.cmd](a)
        return 0
    except Failure as e:
        print(f"hq {a.cmd} failed: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
