#!/usr/bin/env python3
"""HQ page files for hq.sectionnine.dev, written into the directory the hq container serves.

  hq-status.py status   write status.json (every 2 min): what each agent is doing,
                        from the issues in section9-hq, plus the month's OpenRouter usage
  hq-status.py crew     fetch main's crew/ folder from ghost-in-the-machine (every 10 min)

The page is behind Cloudflare Access, but its text is still allowlisted by default:
an issue labelled `private`, or whose title matches a term in <secrets-dir>/crew-deny.txt
(one per line), shows as "On a private task". Without that file nothing is known to be
safe, so only agent states and "On a task" are published. No money amounts, only a
percentage. status.json keeps the format of crew/status.json in ghost-in-the-machine,
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
PUBLIC_URLS = (f"https://github.com/{CREW_REPO}/pull/", "https://petterfranzen.github.io/ghost-in-the-machine/")
URL = re.compile(r"https://(?:github\.com/petterfranzen/ghost-in-the-machine/pull/\d+|petterfranzen\.github\.io/ghost-in-the-machine/[\w./-]*)")
KEYS = re.compile(r"(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}
DENY_BASE = r"petter|franz[eé]n"     # the owner's name never shows, whatever the deny file says
PRIVATE, ON_TASK = "On a private task", "On a task"
RECENT, TEXT_MAX = 5, 90
ZULU = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
MAX_FILE, MAX_TOTAL = 5 << 20, 50 << 20


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = text.replace(s, "[redacted]")
    return KEYS.sub("[redacted]", text)


def read_env_value(path, name):
    value = None
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    for line in text.splitlines():
        line = line.strip().removeprefix("export ").lstrip()
        key, sep, val = line.partition("=")
        if sep and not line.startswith("#") and key.strip() == name:
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            value = val
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


def build(now, agents, issues, comments, key, model, deny):
    """The status document. `deny` is a compiled regex, or None when there is no deny file."""
    labels = lambda i: {l["name"] for l in i.get("labels", [])}
    base = re.compile(DENY_BASE, re.IGNORECASE)

    def text(issue):
        title = issue.get("title") or ""
        if "private" in labels(issue) or base.search(title) or deny.search(title):
            return PRIVATE
        return short(title)

    links = {}
    for c in comments:           # oldest first, so a later comment's link wins
        m = URL.search(c.get("body") or "")
        if m:
            links[c["issue_url"].rsplit("/", 1)[-1]] = m.group(0)

    out = []
    for r in agents:
        mine = [i for i in issues if f"agent:{r['name'].lower()}" in labels(i) and "pull_request" not in i]
        newest = sorted(mine, key=lambda i: i["updated_at"], reverse=True)
        working = [i for i in newest if "status:working" in labels(i)]
        now_text = None
        if working:
            now_text = text(working[0]) if deny else ON_TASK
        recent = []
        for i in newest:
            if deny and labels(i) & {"status:done", "status:review"}:
                item = {"at": iso(i.get("closed_at") or i["updated_at"]), "text": text(i)}
                url = URL.search(i.get("body") or "")
                url = links.get(str(i["number"])) or (url.group(0) if url else None)
                if url and item["text"] != PRIVATE:
                    item["url"] = url
                if recent and item["text"] == PRIVATE == recent[-1]["text"]:
                    continue
                recent.append(item)
        out.append({
            "name": r["name"],
            "role": r.get("title") or r.get("role") or "",
            "state": "working" if working else "idle",
            "now": now_text,
            "lastActiveAt": iso(newest[0]["updated_at"]) if newest else None,
            "budgetPercent": 0,       # per-agent spend isn't tracked on the VPS
            "recent": recent[:RECENT],
        })
    used, cap = key.get("usage_monthly"), key.get("limit")
    doc = {
        "generatedAt": iso(now),
        "budget": {"usedPercent": round(100 * float(used) / float(cap)) if used is not None and cap else 0,
                   "month": now.strftime("%Y-%m")},
        "model": model,
        "agents": out,
    }
    validate(doc, deny)
    return doc


def validate(doc, deny):
    def need(ok, what):
        if not ok:
            raise Failure(f"status.json invalid: {what}")

    need(set(doc) == {"generatedAt", "budget", "model", "agents"} and ZULU.match(doc["generatedAt"]), "top level")
    need(isinstance(doc["budget"]["usedPercent"], int) and doc["budget"]["usedPercent"] >= 0, "budget")
    for a in doc["agents"]:
        need(a["state"] in ("working", "idle") and (a["lastActiveAt"] is None or ZULU.match(a["lastActiveAt"])), "agent")
        for r in a["recent"]:
            need(ZULU.match(r["at"]) and ("url" not in r or r["url"].startswith(PUBLIC_URLS)), "recent item")
    words = [a["name"] for a in doc["agents"]] + [a["now"] or "" for a in doc["agents"]] \
        + [r["text"] for a in doc["agents"] for r in a["recent"]]
    pattern = re.compile(DENY_BASE + ("|" + deny.pattern if deny else ""), re.IGNORECASE)
    need(not any(pattern.search(w) for w in words), "a private term would be published")
    need(not re.search(r"\$\s?\d|cents|usd", json.dumps(doc), re.IGNORECASE), "a money amount would be published")


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


def do_status(a):
    ghtoken = read_env_value(Path(a.secrets_dir) / "sandbox.env", "GH_TOKEN")
    orkey = read_env_value(Path(a.secrets_dir) / "hermes.env", "OPENROUTER_API_KEY")
    secrets.extend([ghtoken, orkey])
    for url in (a.github, a.openrouter):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "https" and u.hostname not in LOOPBACK:
            raise Failure(f"refusing to send a key over plain HTTP to {u.hostname}")
    try:
        agents = tomllib.loads((Path(a.repo_root) / "agents" / "roster.toml").read_text(encoding="utf-8"))["agents"]
        model = model_of((Path(a.repo_root) / "hermes" / "config.yaml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, KeyError) as e:
        raise Failure(f"cannot read the roster or Hermes config: {e}") from None
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    board = f"{a.github.rstrip('/')}/repos/{BOARD}"
    issues = http(f"{board}/issues?state=all&sort=updated&direction=desc&per_page=100", ghtoken) or []
    since = iso(now - dt.timedelta(days=14))
    comments = http(f"{board}/issues/comments?since={since}&per_page=100", ghtoken) or []
    key = (http(a.openrouter.rstrip("/") + "/key", orkey) or {}).get("data") or {}
    doc = build(now, agents, issues, comments, key, model, load_deny(a.secrets_dir))
    write_atomic(Path(a.site_dir) / "status.json", (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode())
    print(f"hq status: {len(doc['agents'])} agents, {sum(x['state'] == 'working' for x in doc['agents'])} working")


def do_crew(a):
    """Mirror main's crew/ into the site dir (never its status.json, which is live)."""
    api = f"{a.github.rstrip('/')}/repos/{CREW_REPO}/git"
    tree = http(f"{api}/trees/main%3Acrew?recursive=1")
    if tree.get("truncated"):
        raise Failure("crew/ listing is truncated")
    site, state = Path(a.site_dir), Path(a.state_dir) / "hq-crew.sha"
    try:
        if state.read_text().strip() == tree["sha"] and (site / "index.html").exists():
            print("hq crew: unchanged")
            return
    except OSError:
        pass
    files, total = {}, 0
    for e in tree["tree"]:
        parts = e["path"].split("/")
        if e["type"] != "blob" or e["path"] == "status.json" or ".." in parts or e["path"].startswith("/"):
            continue
        if e.get("size", 0) > MAX_FILE or (total := total + e.get("size", 0)) > MAX_TOTAL:
            raise Failure(f"crew/{e['path']} is too large")
        files[e["path"]] = e["sha"]
    if "index.html" not in files:
        raise Failure("crew/index.html is missing; keeping what is served")
    for rel, sha in files.items():
        blob = http(f"{api}/blobs/{sha}")
        write_atomic(site / rel, base64.b64decode(blob["content"]))
    for p in sorted(site.rglob("*"), reverse=True):      # files and folders no longer in crew/
        rel = p.relative_to(site).as_posix()
        if rel == "status.json" or rel.endswith(".tmp"):
            continue
        if p.is_file() and rel not in files:
            p.unlink()
        elif p.is_dir() and not any(p.iterdir()):
            p.rmdir()
    write_atomic(state, tree["sha"].encode())
    print(f"hq crew: {len(files)} files")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("cmd", choices=["status", "crew"])
    p.add_argument("--secrets-dir", default="/etc/section9")
    p.add_argument("--state-dir", default="/var/lib/section9")
    p.add_argument("--site-dir", default="/var/lib/section9/hq")
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--github", default="https://api.github.com")
    p.add_argument("--openrouter", default="https://openrouter.ai/api/v1")
    a = p.parse_args(argv)
    try:
        {"status": do_status, "crew": do_crew}[a.cmd](a)
        return 0
    except Failure as e:
        print(f"hq {a.cmd} failed: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
