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


def read_env_value(path, name):
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    value = None
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


def request(method, url, token=None, body=None, timeout=30):
    """(parsed JSON or None, response headers). A public endpoint (OSV, KEV) is called without a token."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    if token:
        req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "section9-sca")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def repo_pins(root):
    """(queries, notes): the repo's version pins we can query, and the ones we cannot.

    A query is (label, ecosystem, OSV package, version, where it came from).
    """
    def read(rel):
        try:
            return (root / rel).read_text(encoding="utf-8")
        except OSError:
            return None

    queries, notes = [], []
    for rel, pattern, label, eco, pkg in PINS:
        m = re.search(pattern, read(rel) or "")
        if m:
            queries.append((label, eco, pkg, m.group(1), rel))
        else:
            notes.append(f"{rel}: the {label} pin was not found")
    sand = read("sandbox/Dockerfile") or ""
    for pattern, what in ((r"(?m)^FROM (\S+)", "sandbox base image"),
                          (r"(?m)^ARG NODE_VERSION=(\S+)", "Node runtime"),
                          (r"(?m)^ARG PONYTAIL_TAG=(\S+)", "Ponytail skill")):
        m = re.search(pattern, sand)
        if m:
            notes.append(f"{what} {m.group(1)} (pinned by hand or a digest, so not queried by version)")
    m = re.search(r"(?m)^\s*image:\s*(nousresearch/hermes-agent:\S+)", read("vps/compose.yaml") or "")
    if m:
        notes.append(f"Hermes image {m.group(1)} (our own image, not queried)")
    port = read("portfolio/Dockerfile")
    if port is not None:
        notes += [f"portfolio base image {m.group(1)} (not queried by version)"
                  for m in re.finditer(r"(?m)^FROM (\S+)", port)]
    return queries, notes


def host_pins():
    """(queries, notes) for this host's own versions: the docker stack and openssh-server.

    docker, runc and containerd are Go programs (OSV's Go database carries their advisories);
    openssh-server is an Ubuntu package, and OSV's Ubuntu database indexes it by source name.
    """
    queries, notes = [], []
    out = run(["docker", "version"])
    versions = re.findall(r"(?m)Version:\s*v?([0-9][\w.+-]*)", out or "")
    if versions:
        queries.append(("docker", "Go", "github.com/docker/docker", versions[-1], "docker version"))
    else:
        notes.append("docker version: not available")
    out = run(["runc", "--version"])
    m = re.search(r"runc version v?([0-9][\w.+-]*)", out or "")
    if m:
        queries.append(("runc", "Go", "github.com/opencontainers/runc", m.group(1), "runc --version"))
    else:
        notes.append("runc --version: not available")
    out = run(["containerd", "--version"])
    m = re.search(r"(?m)^containerd\b.*?\bv?([0-9]+\.[0-9][\w.+-]*)", out or "")
    if m:
        queries.append(("containerd", "Go", "github.com/containerd/containerd", m.group(1), "containerd --version"))
    else:
        notes.append("containerd --version: not available")
    out = run(["dpkg", "-s", "openssh-server"])
    version = re.search(r"(?m)^Version:\s*(\S+)", out or "")
    source = re.search(r"(?m)^Source:\s*(\S+)", out or "")
    if version:
        queries.append(("openssh-server", "Ubuntu", source.group(1) if source else "openssh-server",
                        version.group(1), "dpkg -s openssh-server"))
    else:
        notes.append("dpkg -s openssh-server: not available")
    return queries, notes


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


def score_of(detail):
    """(score, how). A CVSS-v3 vector if the record carries one, else a label's floor, else None.

    Many Go and Ubuntu records carry no vector; their GitHub (GHSA) or Ubuntu sibling does, and
    grouping by CVE picks that sibling. A bare 'high'/'critical' label is treated as the 7.0 floor.
    """
    for sev in detail.get("severity") or []:
        if str(sev.get("type", "")).startswith("CVSS_V3"):
            s = cvss_base(sev.get("score", ""))
            if s is not None:
                return s, "vector"
    labels = [sev.get("score") for sev in detail.get("severity") or [] if sev.get("type") == "Ubuntu"]
    labels.append((detail.get("database_specific") or {}).get("severity"))
    for lab in labels:
        if str(lab).strip().lower() in ("critical", "high"):
            return CVSS_MIN, str(lab).strip().lower()
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


def findings(queries, reply, kev, fetch):
    """(rows, dropped): one row per CVE (the CVE is what a fix and a KEV entry key on)."""
    hits = {}
    for q, res in zip(queries, reply.get("results", [])):
        for v in (res or {}).get("vulns", []):
            hits.setdefault(v["id"], q)
    if len(hits) > MAX_FETCH:
        raise Failure(f"{len(hits)} advisories is over the {MAX_FETCH} cap; refusing to fetch that many")
    details = {vid: fetch(vid) for vid in hits}
    groups = {}
    for vid, q in hits.items():
        groups.setdefault((q[0], cve_key(details[vid])), []).append((q, details[vid]))
    rows, dropped = [], 0
    for (label, _cve), members in groups.items():
        version = members[0][0][3]
        best = max((d for _, d in members),
                   key=lambda d: 2 if score_of(d)[1] == "vector" else (1 if score_of(d)[0] else 0))
        keep, score = judge(best, kev)
        if not keep:
            dropped += 1
            continue
        rows.append({"component": label, "version": version, "id": best.get("id"), "score": score,
                     "fix": fixed_for(best, version), "published": (best.get("published") or "")[:10]})
    return sorted(rows, key=lambda r: (r["component"], str(r["id"]))), dropped


def kev_set(catalog):
    """The CISA KEV catalogue's CVE ids, or an empty set when it could not be read."""
    ids = {v.get("cveID") for v in (catalog or {}).get("vulnerabilities", []) if v.get("cveID")}
    return ids


def image_scan():
    """One line for the optional monthly image layer: a trivy/grype scan, or what to install."""
    tool = next((t for t in ("trivy", "grype") if shutil.which(t)), None)
    if tool is None:
        return ("no image scan: install trivy (its own apt repo, or `snap install trivy`) or grype to add the "
                "monthly layer, then run this with --images")
    refs = sorted(set((run(["docker", "ps", "--format", "{{.Image}}"]) or "").split()))
    if not refs:
        return f"{tool} is installed but `docker ps` gave no images; image scan skipped."
    counts = []
    for ref in refs:
        args = ([tool, "image", "--quiet", "--severity", "HIGH,CRITICAL", "--format", "json", ref] if tool == "trivy"
                else [tool, "image", ref, "-o", "json"])
        try:
            p = subprocess.run(args, capture_output=True, text=True, timeout=600)
            out = json.loads(p.stdout or "{}")
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            counts.append(f"{ref}: scan failed ({redact(str(e))[:60]})")
            continue
        hits = (sum(len(r.get("Vulnerabilities") or []) for r in out.get("Results", [])) if tool == "trivy"
                else len(out.get("matches") or []))
        counts.append(f"{ref}: {hits} HIGH/CRITICAL")
    return f"{tool} image scan (HIGH/CRITICAL): " + "; ".join(counts) + "."


def body(rows, queries, notes, dropped, now, kev_ok, images=None):
    lines = [f"**Supply-chain scan, run by the VPS weekly.** As of {now:%Y-%m-%d %H:%M} UTC.", ""]
    if rows:
        lines += [f"**{len(rows)} finding(s) at CVSS 7.0+ or on the CISA KEV list.**", "",
                  "| component | version | advisory | CVSS | fixed in | fix published |", "|---|---|---|---:|---|---|"]
        for r in rows:
            lines.append(f"| {r['component']} | {r['version']} | [{r['id']}](https://osv.dev/vulnerability/{r['id']})"
                         f"{' (KEV)' if r['score'] == 'KEV' else ''} | {r['score']} | {r['fix'] or '—'} | "
                         f"{r['published'] or '—'} |")
        lines += ["", "The fix column is the earliest fixed release above the version we run; the date is that "
                  "advisory's OSV published time, the closest OSV gives to the fix's own release.", ""]
    else:
        lines += [f"**Clear, {now:%Y-%m-%d}:** no advisory at CVSS 7.0 or higher, and none on the CISA KEV "
                  "list, for any pin below.", ""]
    lines += ["**Pins checked:**"]
    lines += [f"- {label} {v} — {eco} {pkg} (from {src})" for label, eco, pkg, v, src in queries]
    if dropped:
        lines.append(f"{dropped} advisory/advisories below CVSS 7.0 and not on the CISA KEV list was/were dropped.")
    if not kev_ok:
        lines.append("The CISA KEV list could not be read this run, so KEV-only advisories may be missing.")
    if notes:
        lines += ["**Not queried by version:**"] + [f"- {n}" for n in notes]
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
        queries, notes = repo_pins(Path(a.repo_root))
        hq, hn = host_pins()
        queries, notes = queries + hq, notes + hn
        if not queries:
            raise Failure("no version pins to query")
        log(f"pins: {len(queries)} queried, {len(notes)} noted")
        try:
            kev, kev_ok = kev_set(http("GET", a.kev)), True
        except Failure as e:
            log(f"kev: {e}")
            kev, kev_ok = set(), False
        reply = http("POST", a.osv.rstrip("/") + "/querybatch",
                     body={"queries": [{"package": {"ecosystem": q[1], "name": q[2]}, "version": q[3]}
                                       for q in queries]})
        rows, dropped = findings(queries, reply or {},
                                 kev, lambda vid: http("GET", a.osv.rstrip("/") + "/vulns/" + vid))
        log(f"findings: {len(rows)} kept, {dropped} dropped")
        text = body(rows, queries, notes, dropped, dt.datetime.now(dt.timezone.utc), kev_ok,
                    image_scan() if a.images else None)
        post(a, board, text, log)
        return 0
    except Failure as e:
        print(f"sca-watch: {redact(str(e))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
