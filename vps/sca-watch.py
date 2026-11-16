#!/usr/bin/env python3
"""Section 9 supply-chain watch: one weekly pass over the version pins around the stack.

  sca-watch.py            query OSV for every repo pin and the host's own versions, keep
                          CVSS 7.0+ or CISA KEV, and rewrite the open "Supply-chain" issue
  sca-watch.py --images   also scan the running images with trivy or grype, when one is installed

Standard library only. The GitHub token comes from a root-only file and is never printed.
Run by section9-sca.timer (weekly, Monday 05:30 UTC). See docs/RUNBOOK.md.
"""

import argparse
import datetime as dt
import json
import math
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = "petterfranzen/section9-hq"
OSV = "https://api.osv.dev/v1"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
KEYS = re.compile(r"(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
CVSS_MIN = 7.0
MAX_FETCH = 300          # cap on advisory detail fetches per run; refuse to hammer OSV past it
PER_PAGE, MAX_PAGES = 100, 20
TIMEOUT = 30             # seconds, for every HTTP call

# The repo pins we can query by version: (file, regex, label, ecosystem, OSV package). The version
# is the regex's group 1. Only what OSV indexes by version is here; a digest or a runtime is not.
PINS = (
    ("sandbox/Dockerfile", r"(?m)^ARG GITLEAKS_VERSION=(\S+)", "gitleaks", "Go", "github.com/gitleaks/gitleaks"),
    ("sandbox/Dockerfile", r"(?m)^ARG MISE_VERSION=(\S+)", "mise", "Go", "github.com/jdx/mise"),
    ("sandbox/Dockerfile", r"(?m)^ARG PLAYWRIGHT_VERSION=(\S+)", "playwright", "npm", "playwright"),
    ("vps/compose.yaml", r"(?m)^\s*image:\s*caddy:([0-9][\w.]*)", "caddy", "Go", "github.com/caddyserver/caddy"),
)


class Failure(Exception):
    pass


secrets = []


def redact(text):
    for s in secrets:
        text = str(text).replace(s, "[redacted]")
    return KEYS.sub("[redacted]", str(text))


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


def new_request(method, url, token, body):
    req = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "section9-sca")
    if body is not None:
        req.add_header("Content-Type", "application/json")
    return req


def request(method, url, token=None, body=None):
    """(parsed JSON or None, response headers). A public endpoint (OSV, KEV) is called without a token."""
    req = new_request(method, url, token, body)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw, headers = resp.read(), resp.headers
    except urllib.error.HTTPError as e:
        raise Failure(f"{method} {redact(url)} -> {e.code}: {redact(e.read().decode('utf-8', 'replace'))[:200]}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise Failure(f"{method} {redact(url)} -> no response: {redact(getattr(e, 'reason', e))}") from None
    try:
        return (json.loads(raw) if raw.strip() else None), headers
    except ValueError:
        raise Failure(f"{method} {redact(url)} -> reply is not JSON") from None


def http(*args, **kwargs):
    return request(*args, **kwargs)[0]


def next_link(headers):
    for part in (headers.get("Link") or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;.*\brel="?next"?', part)
        if m:
            return m.group(1)
    return None


class Board:
    def __init__(self, api, token):
        self.api, self.token = api.rstrip("/"), token

    def call(self, method, path, body=None):
        return http(method, f"{self.api}/repos/{REPO}{path}", self.token, body)

    def pages(self, path):
        url = f"{self.api}/repos/{REPO}{path}{'&' if '?' in path else '?'}per_page={PER_PAGE}"
        items = []
        for _ in range(MAX_PAGES):
            page, headers = request("GET", url, self.token)
            items += page or []
            url = next_link(headers)
            if not url:
                return items
        raise Failure(f"GET {path} -> more than {MAX_PAGES * PER_PAGE} items; refusing to work from a partial list")


def run(cmd):
    """The command's stdout, or None when it is not installed or exits non-zero."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


# What sandbox/Dockerfile pins by hand or by digest: noted in the issue, not queried by version.
SANDBOX_NOTED = ((r"(?m)^FROM (\S+)", "sandbox base image"),
                 (r"(?m)^ARG NODE_VERSION=(\S+)", "Node runtime"),
                 (r"(?m)^ARG PONYTAIL_TAG=(\S+)", "Ponytail skill"))


def read_or_none(path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def pin_of(root, pin):
    """(query, None) for a pin found in its file, else (None, note)."""
    rel, pattern, label, eco, pkg = pin
    m = re.search(pattern, read_or_none(root / rel) or "")
    if m:
        return (label, eco, pkg, m.group(1), rel), None
    return None, f"{rel}: the {label} pin was not found"


def unqueried(root):
    """Notes for what the repo pins in a way OSV cannot be asked about by version."""
    sand = read_or_none(root / "sandbox/Dockerfile") or ""
    found = [(re.search(pattern, sand), what) for pattern, what in SANDBOX_NOTED]
    notes = [f"{what} {m.group(1)} (pinned by hand or a digest, so not queried by version)" for m, what in found if m]
    m = re.search(r"(?m)^\s*image:\s*(nousresearch/hermes-agent:\S+)", read_or_none(root / "vps/compose.yaml") or "")
    if m:
        notes.append(f"Hermes image {m.group(1)} (our own image, not queried)")
    port = read_or_none(root / "portfolio/Dockerfile") or ""
    return notes + [f"portfolio base image {m.group(1)} (not queried by version)"
                    for m in re.finditer(r"(?m)^FROM (\S+)", port)]


def repo_pins(root):
    """(queries, notes): the repo's version pins we can query, and the ones we cannot.

    A query is (label, ecosystem, OSV package, version, where it came from).
    """
    found = [pin_of(root, pin) for pin in PINS]
    queries = [query for query, _ in found if query]
    notes = [note for _, note in found if note]
    return queries, notes + unqueried(root)


def host_pins():
    """(queries, notes) for this host's own versions: the docker stack and openssh-server.

    docker, runc and containerd are Go programs (OSV's Go database carries their advisories);
    openssh-server is an Ubuntu package, and OSV's Ubuntu database indexes it by source name.
    """
    queries, notes = [], []
    for pin in (docker_pin, runc_pin, containerd_pin, openssh_pin):
        query, note = pin()
        if query:
            queries.append(query)
        else:
            notes.append(note)
    return queries, notes


def docker_pin():
    """(query, None), or (None, note) when docker is not there."""
    # The engine's version, not a bundled component's: `docker version`'s text output ends with
    # docker-init (0.19.0), so taking the last `Version:` line pinned docker-init against
    # github.com/docker/docker and returned a page of ancient false positives. Ask the server first.
    out = run(["docker", "version", "--format", "{{.Server.Version}}"])
    m = re.fullmatch(r"v?([0-9][\w.+-]*)\s*", out or "")
    if not m:  # an old client that cannot format: read the first Version: inside the Server block
        m = re.search(r"(?s)\bServer\b.*?\n\s*Version:\s*v?([0-9][\w.+-]*)",
                      run(["docker", "version"]) or "")
    if m:
        return ("docker", "Go", "github.com/docker/docker", m.group(1), "docker version"), None
    return None, "docker version: not available"


def runc_pin():
    """(query, None), or (None, note) when runc is not there."""
    out = run(["runc", "--version"])
    m = re.search(r"runc version v?([0-9][\w.+-]*)", out or "")
    if m:
        return ("runc", "Go", "github.com/opencontainers/runc", m.group(1), "runc --version"), None
    return None, "runc --version: not available"


def containerd_pin():
    out = run(["containerd", "--version"])
    m = re.search(r"(?m)^containerd\b.*?\bv?([0-9]+\.[0-9][\w.+-]*)", out or "")
    if m:
        return ("containerd", "Go", "github.com/containerd/containerd", m.group(1), "containerd --version"), None
    return None, "containerd --version: not available"


def openssh_pin():
    out = run(["dpkg", "-s", "openssh-server"])
    version = re.search(r"(?m)^Version:\s*(\S+)", out or "")
    source = re.search(r"(?m)^Source:\s*(\S+)", out or "")
    if version:
        return ("openssh-server", "Ubuntu", source.group(1) if source else "openssh-server",
                version.group(1), "dpkg -s openssh-server"), None
    return None, "dpkg -s openssh-server: not available"


_AV = {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}
_AC = {"L": 0.77, "H": 0.44}
_PR_U = {"N": 0.85, "L": 0.62, "H": 0.27}
_PR_C = {"N": 0.85, "L": 0.68, "H": 0.5}
_UI = {"N": 0.85, "R": 0.62}
_CIA = {"H": 0.56, "L": 0.22, "N": 0.0}


def roundup(x):
    """CVSS v3.1's Roundup: up to one decimal, away from zero, without float drift."""
    i = int(round(x * 100000))
    return i / 100000.0 if i % 10000 == 0 else (math.floor(i / 10000) + 1) / 10.0


def cvss_base(vector):
    """The CVSS v3.0/v3.1 base score of a vector string, or None for anything else (v2, v4)."""
    if not (vector or "").startswith("CVSS:3."):
        return None
    m = dict(p.split(":", 1) for p in vector.split("/")[1:] if ":" in p)
    try:
        scope_changed = m["S"] == "C"
        pr = (_PR_C if scope_changed else _PR_U)[m["PR"]]
        iss = 1 - (1 - _CIA[m["C"]]) * (1 - _CIA[m["I"]]) * (1 - _CIA[m["A"]])
        impact = (7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15) if scope_changed else 6.42 * iss
        exploit = 8.22 * _AV[m["AV"]] * _AC[m["AC"]] * pr * _UI[m["UI"]]
    except (KeyError, ValueError):
        return None
    if impact <= 0:
        return 0.0
    return roundup(min((impact + exploit) * (1.08 if scope_changed else 1.0), 10.0))


def vector_score(detail):
    """The base score of the record's first CVSS-v3 vector that can be read, or None."""
    vectors = [sev.get("score", "") for sev in detail.get("severity") or []
               if str(sev.get("type", "")).startswith("CVSS_V3")]
    scores = (cvss_base(vector) for vector in vectors)
    return next((s for s in scores if s is not None), None)


def severe_label(detail):
    """The record's first 'critical' or 'high' label (Ubuntu's, then the database's own), or None."""
    labels = [sev.get("score") for sev in detail.get("severity") or [] if sev.get("type") == "Ubuntu"]
    labels.append((detail.get("database_specific") or {}).get("severity"))
    words = (str(lab).strip().lower() for lab in labels)
    return next((w for w in words if w in ("critical", "high")), None)


def score_of(detail):
    """(score, how). A CVSS-v3 vector if the record carries one, else a label's floor, else None.

    Many Go and Ubuntu records carry no vector; their GitHub (GHSA) or Ubuntu sibling does, and
    grouping by CVE picks that sibling. A bare 'high'/'critical' label is treated as the 7.0 floor.
    """
    score = vector_score(detail)
    if score is not None:
        return score, "vector"
    label = severe_label(detail)
    if label:
        return CVSS_MIN, label
    return None, "no CVSS"


def version_key(v):
    """A rough sort key: digits as ints, the rest as strings, the leading v and an epoch dropped.

    Enough to order the versions this scan compares (semver and Debian); not a full version parser.
    """
    v = re.sub(r"^\d+:", "", str(v).strip().lstrip("vV"))
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"([0-9]+)", v) if p != "")


def fixed_for(detail, current):
    """The earliest fixed release above the version we run, from the record's affected ranges."""
    fixed = [ev["fixed"] for aff in detail.get("affected", []) for rng in aff.get("ranges", [])
             for ev in rng.get("events", []) if "fixed" in ev]
    if not fixed:
        return None
    above = [f for f in fixed if version_key(f) > version_key(current)]
    return min(above or fixed, key=version_key)


def cve_key(detail):
    for x in [*(detail.get("aliases") or []), detail.get("id", "")]:
        if str(x).startswith("CVE-"):
            return str(x)
    return detail.get("id")


def judge(detail, kev):
    """(keep, score text): CVSS 7.0+, or a KEV listing of any severity."""
    cves = [x for x in [detail.get("id", ""), *(detail.get("aliases") or [])] if str(x).startswith("CVE-")]
    if any(c in kev for c in cves):
        return True, "KEV"
    score, how = score_of(detail)
    if score is None:
        return False, "no CVSS"
    return (score >= CVSS_MIN), (f"{score:.1f}" if how == "vector" else f"{how} (label)")


def advisory_hits(queries, reply):
    """advisory id -> the first query OSV matched it to."""
    hits = {}
    for q, res in zip(queries, reply.get("results", [])):
        for v in (res or {}).get("vulns", []):
            hits.setdefault(v["id"], q)
    return hits


def evidence(detail):
    """How much a record says about its severity: 2 a vector, 1 a label, 0 nothing."""
    score, how = score_of(detail)
    if how == "vector":
        return 2
    return 1 if score else 0


def finding(label, members, kev):
    """The row for one CVE of one component, from its best-scored record; None when it is dropped."""
    version = members[0][0][3]
    best = max((d for _, d in members), key=evidence)
    keep, score = judge(best, kev)
    if not keep:
        return None
    return {"component": label, "version": version, "id": best.get("id"), "score": score,
            "fix": fixed_for(best, version), "published": (best.get("published") or "")[:10]}


def findings(queries, reply, kev, fetch):
    """(rows, dropped): one row per CVE (the CVE is what a fix and a KEV entry key on)."""
    hits = advisory_hits(queries, reply)
    if len(hits) > MAX_FETCH:
        raise Failure(f"{len(hits)} advisories is over the {MAX_FETCH} cap; refusing to fetch that many")
    details = {vid: fetch(vid) for vid in hits}
    groups = {}
    for vid, q in hits.items():
        groups.setdefault((q[0], cve_key(details[vid])), []).append((q, details[vid]))
    found = [finding(label, members, kev) for (label, _cve), members in groups.items()]
    rows = [row for row in found if row]
    return sorted(rows, key=lambda r: (r["component"], str(r["id"]))), len(found) - len(rows)


def kev_set(catalog):
    """The CISA KEV catalogue's CVE ids, or an empty set when it could not be read."""
    ids = {v.get("cveID") for v in (catalog or {}).get("vulnerabilities", []) if v.get("cveID")}
    return ids


def scan_args(tool, ref):
    if tool == "trivy":
        return [tool, "image", "--quiet", "--severity", "HIGH,CRITICAL", "--format", "json", ref]
    return [tool, "image", ref, "-o", "json"]


def scan_hits(tool, out):
    if tool == "trivy":
        return sum(len(r.get("Vulnerabilities") or []) for r in out.get("Results", []))
    return len(out.get("matches") or [])


def scan_image(tool, ref):
    """One image's count of HIGH/CRITICAL findings, or why the scan failed."""
    try:
        p = subprocess.run(scan_args(tool, ref), capture_output=True, text=True, timeout=600)
        out = json.loads(p.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        return f"{ref}: scan failed ({redact(str(e))[:60]})"
    return f"{ref}: {scan_hits(tool, out)} HIGH/CRITICAL"


def image_scan():
    """One line for the optional monthly image layer: a trivy/grype scan, or what to install."""
    tool = next((t for t in ("trivy", "grype") if shutil.which(t)), None)
    if tool is None:
        return ("no image scan: install trivy (its own apt repo, or `snap install trivy`) or grype to add the "
                "monthly layer, then run this with --images")
    refs = sorted(set((run(["docker", "ps", "--format", "{{.Image}}"]) or "").split()))
    if not refs:
        return f"{tool} is installed but `docker ps` gave no images; image scan skipped."
    counts = [scan_image(tool, ref) for ref in refs]
    return f"{tool} image scan (HIGH/CRITICAL): " + "; ".join(counts) + "."


def finding_lines(rows, now):
    """The table of findings, or the line that says the scan is clear."""
    if not rows:
        return [f"**Clear, {now:%Y-%m-%d}:** no advisory at CVSS 7.0 or higher, and none on the CISA KEV "
                "list, for any pin below.", ""]
    lines = [f"**{len(rows)} finding(s) at CVSS 7.0+ or on the CISA KEV list.**", "",
             "| component | version | advisory | CVSS | fixed in | fix published |", "|---|---|---|---:|---|---|"]
    for r in rows:
        lines.append(f"| {r['component']} | {r['version']} | [{r['id']}](https://osv.dev/vulnerability/{r['id']})"
                     f"{' (KEV)' if r['score'] == 'KEV' else ''} | {r['score']} | {r['fix'] or '—'} | "
                     f"{r['published'] or '—'} |")
    return lines + ["", "The fix column is the earliest fixed release above the version we run; the date is that "
                    "advisory's OSV published time, the closest OSV gives to the fix's own release.", ""]


def pin_lines(scan):
    """What was checked, what was dropped and what could not be queried."""
    lines = ["**Pins checked:**"]
    lines += [f"- {label} {v} — {eco} {pkg} (from {src})" for label, eco, pkg, v, src in scan["queries"]]
    if scan["dropped"]:
        lines.append(f"{scan['dropped']} advisory/advisories below CVSS 7.0 and not on the CISA KEV list was/were dropped.")
    if not scan["kev_ok"]:
        lines.append("The CISA KEV list could not be read this run, so KEV-only advisories may be missing.")
    if scan["notes"]:
        lines += ["**Not queried by version:**"] + [f"- {n}" for n in scan["notes"]]
    return lines


def body(scan, now, images=None):
    """The issue text. `scan` holds rows, dropped, queries, notes and kev_ok."""
    lines = [f"**Supply-chain scan, run by the VPS weekly.** As of {now:%Y-%m-%d %H:%M} UTC.", ""]
    lines += finding_lines(scan["rows"], now) + pin_lines(scan)
    lines += ["", images if images else "Rewritten by `vps/sca-watch.py`; don't edit. Leave this issue open and pinned."]
    return "\n".join(lines)


def post(a, board, text, log):
    """Rewrite the open 'Supply-chain' issue, or open and pin it on first use."""
    open_ = board.pages("/issues?state=open&sort=created&direction=asc")
    issue = next((i for i in open_ if i["title"] == "Supply-chain" and "pull_request" not in i), None)
    if issue:
        board.call("PATCH", f"/issues/{issue['number']}", {"body": text})
        log(f"supply-chain: updated #{issue['number']}")
        return
    issue = board.call("POST", "/issues", {"title": "Supply-chain", "body": text})
    log(f"supply-chain: opened #{issue['number']}")
    try:        # pinning is GraphQL-only and needs more than issue access; best effort
        http("POST", f"{board.api}/graphql", board.token, {
            "query": "mutation($id: ID!) { pinIssue(input: {issueId: $id}) { issue { number } } }",
            "variables": {"id": issue["node_id"]}})
    except (Failure, KeyError) as e:
        log(f"supply-chain: could not pin #{issue['number']} (pin it by hand): {e}")


def read_kev(url, log):
    """(the KEV ids, True), or (an empty set, False) when the catalogue could not be read."""
    try:
        return kev_set(http("GET", url)), True
    except Failure as e:
        log(f"kev: {e}")
        return set(), False


def scan_pins(a, log):
    """Query OSV for every pin. Returns what `body` writes up."""
    queries, notes = repo_pins(Path(a.repo_root))
    hq, hn = host_pins()
    queries, notes = queries + hq, notes + hn
    if not queries:
        raise Failure("no version pins to query")
    log(f"pins: {len(queries)} queried, {len(notes)} noted")
    kev, kev_ok = read_kev(a.kev, log)
    reply = http("POST", a.osv.rstrip("/") + "/querybatch",
                 body={"queries": [{"package": {"ecosystem": q[1], "name": q[2]}, "version": q[3]}
                                   for q in queries]})
    rows, dropped = findings(queries, reply or {},
                             kev, lambda vid: http("GET", a.osv.rstrip("/") + "/vulns/" + vid))
    log(f"findings: {len(rows)} kept, {dropped} dropped")
    return {"rows": rows, "dropped": dropped, "queries": queries, "notes": notes, "kev_ok": kev_ok}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--secrets-dir", default="/etc/section9")
    p.add_argument("--state-dir", default="/var/lib/section9")
    p.add_argument("--repo-root", default=str(ROOT))
    p.add_argument("--github", default="https://api.github.com")
    p.add_argument("--osv", default=OSV)
    p.add_argument("--kev", default=KEV_URL)
    p.add_argument("--images", action="store_true", help="also scan the running images with trivy or grype")
    a = p.parse_args(argv)

    def log(msg):
        print(redact(msg), flush=True)

    try:
        token = read_env_value(Path(a.secrets_dir) / "sandbox.env", "GH_TOKEN")
        secrets.append(token)
        board = Board(a.github, token)
        scan = scan_pins(a, log)
        text = body(scan, dt.datetime.now(dt.timezone.utc), image_scan() if a.images else None)
        post(a, board, text, log)
        return 0
    except Failure as e:
        print(f"sca-watch: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
