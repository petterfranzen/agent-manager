"""Stub GitHub, Hermes, OpenRouter, OSV and KEV: one in-memory server the scripts under test talk to."""

import base64
import datetime as dt
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

GH = "github_pat_" + "g" * 20
ORKEY = "sk-or-" + "k" * 20
HKEY = "hermes-" + "h" * 20
PREFIX = "/gh/repos/petterfranzen/section9-hq"
CREW_GIT = "/gh/repos/petterfranzen/ghost-in-the-machine/git/"
CHAT = "/hermes/v1/chat/completions"


def zulu(when):
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def tree_of(files, sha, prefix):
    """A git tree listing of a {path: content} folder, as GitHub's API gives it."""
    return {"sha": sha, "truncated": False, "tree": [
        {"path": n, "type": "blob", "sha": prefix + n.replace("/", "-"), "size": len(c)} for n, c in files.items()]}


def blob_of(files, name):
    """The blob whose sha ends in `name` (the path with its slashes as dashes)."""
    content = next(c for n, c in files.items() if n.replace("/", "-") == name)
    return {"content": base64.b64encode(content.encode()).decode()}


class Call:
    """One request to the stub."""

    def __init__(self, method, url, body, headers):
        u = urlsplit(url)
        self.method, self.path, self.query = method, u.path, parse_qs(u.query)
        self.body, self.headers = body, headers
        self.auth = headers.get("Authorization")

    def at(self, path):
        """The same request for another path."""
        other = Call(self.method, path, self.body, self.headers)
        other.query = self.query
        return other

    def param(self, name, default):
        return self.query.get(name, [default])[0]


class Stub:
    def __init__(self):
        self.issues, self.comments, self.hermes_calls = {}, [], []
        self.remaining = 15.0
        self.monthly, self.daily, self.bump = 5.0, 0.25, 0.0
        self.key = None             # None: the normal /key reply; else the reply body to send
        self.key_calls, self.key_fail_after = 0, None   # key_fail_after: fail /key reads after this many
        self.page_size, self.link = None, None     # page_size: serve issue lists in pages of this size
        self.hermes = "report"      # report | silent | slow | error
        self.hermes_model, self.hermes_runtime = None, None   # what the reply echoes / served
        self.next = 1
        self.gh_comments, self.pinned, self.crew_auth = [], None, "unset"
        self.crew_sha, self.checks = "tree1", []
        self.hq_sha, self.hq = "hq1", {"ghost.yaml": "name: ghost\nlabel: Batou's\n", "notes.md": "x", "sub/deep.yaml": "name: no\n"}
        self.crew = {"index.html": "<h1>crew</h1>", "status.json": "SAMPLE", "css/app.css": "a{}"}
        self.osv_batch, self.osv_vulns, self.osv_calls = {"results": []}, {}, []
        self.kev = {"catalogVersion": "test", "vulnerabilities": []}
        self.osv_auth = "unset"
        self.base = ""              # the server's own URL, for Link headers

    def add(self, labels, title="Do a thing", state="open", age_min=None):
        n = self.next
        self.next += 1
        updated = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=age_min or 0)
        # without age_min: a fixed stamp, or "now" for status:working
        fixed = age_min is None and "status:working" not in labels
        self.issues[n] = {"number": n, "title": title, "body": f"body of {n}", "state": state,
                          "created_at": zulu(updated),
                          "updated_at": "2026-11-01T10:00:00Z" if fixed else zulu(updated),
                          "html_url": f"https://github.com/petterfranzen/section9-hq/issues/{n}",
                          "labels": [{"name": x} for x in labels]}
        return n

    def todo_count(self):
        return sum("status:todo" in self.labels(n) for n in self.issues)

    def labels(self, n):
        return {x["name"] for x in self.issues[n]["labels"]}

    def set_labels(self, n, labels):
        self.issues[n]["labels"] = [{"name": x} for x in labels]

    def gh(self, call):
        if call.auth != "Bearer " + GH:
            return 401, {"message": "Bad credentials"}
        if call.path == "/gh/graphql":
            self.pinned = call.body["variables"]["id"]
            return 200, {"data": {}}
        rest = call.path[len(PREFIX):]
        route = self.GH_ROUTES.get((call.method, rest))
        if route:
            return route(self, call)
        return self.gh_issue(call, rest.strip("/").split("/"))

    def gh_comments_list(self, call):
        return 200, self.gh_comments

    def gh_list(self, call):
        want, st = call.param("labels", ""), call.param("state", "open")
        found = [i for i in self.issues.values() if st in (i["state"], "all")
                 and (not want or want in self.labels(i["number"]))]
        size = min(int(call.param("per_page", "30")), self.page_size or 10**6)
        page = int(call.param("page", "1"))
        if page * size < len(found):      # GitHub's Link header, absolute URL
            q = {k: v[0] for k, v in call.query.items()}
            q.update(page=page + 1, per_page=size)
            self.link = f'<{self.base}{call.path}?{urlencode(q)}>; rel="next"'
        return 200, found[(page - 1) * size:page * size]

    def gh_create(self, call):
        n = self.add(call.body.get("labels", []), call.body["title"])
        self.issues[n]["body"] = call.body["body"]
        self.issues[n]["node_id"] = f"node-{n}"
        return 201, self.issues[n]

    GH_ROUTES = {("GET", "/issues/comments"): gh_comments_list, ("GET", "/issues"): gh_list,
                 ("POST", "/issues"): gh_create}

    def known_issue(self, parts):
        """The issue an /issues/<n>[/comments] path names, or None."""
        named = parts[0] == "issues" and len(parts) >= 2
        return self.issues.get(int(parts[1])) if named else None

    def gh_issue(self, call, parts):
        issue = self.known_issue(parts)
        if issue is None:
            return 404, {"message": "Not Found", "echo": call.auth}
        if len(parts) == 3 and call.method == "POST":
            self.comments.append((issue["number"], call.body["body"]))
            return 201, {"id": 1}
        if call.method == "PATCH":
            self.patch(issue, call.body)
        return 200, issue

    def patch(self, issue, body):
        if "labels" in body:
            issue["labels"] = [{"name": x} for x in body["labels"]]
        issue["state"] = body.get("state", issue["state"])
        issue["body"] = body.get("body", issue["body"])
        # GitHub moves updated_at on every change; the dispatcher's stale sweep reads it.
        issue["updated_at"] = zulu(dt.datetime.now(dt.timezone.utc))

    def crew_git(self, call):
        """ghost-in-the-machine's git API: the crew/ and hq/ trees and their blobs."""
        self.crew_auth = call.auth
        path = call.path
        if path.endswith("/trees/main%3Ahq"):
            if self.hq is None:
                return 404, {"message": "Not Found"}
            return 200, tree_of(self.hq, self.hq_sha, "sha-hq-")
        if "/blobs/sha-hq-" in path:
            return 200, blob_of(self.hq, path.rsplit("/sha-hq-", 1)[1])
        if path.endswith("/trees/main%3Acrew"):
            return 200, tree_of(self.crew, self.crew_sha, "sha-")
        return 200, blob_of(self.crew, path.rsplit("/sha-", 1)[1])

    def ci(self, call):
        if call.path.endswith("/check-runs"):
            return 200, {"total_count": len(self.checks), "check_runs": self.checks}
        return self.gh(call.at("/gh" + call.path[len("/ci"):]))

    def or_key(self, call):
        self.key_calls += 1
        if self.key_fail_after is not None and self.key_calls > self.key_fail_after:
            return 500, {"error": "key read refused"}
        if self.key is not None:
            return 200, self.key
        return 200, {"data": {"usage": self.monthly, "limit": 20, "limit_remaining": self.remaining,
                              "usage_monthly": self.monthly, "usage_daily": self.daily}}

    def osv_query(self, call):
        self.osv_auth = call.auth
        self.osv_calls.append(call.body)
        return 200, self.osv_batch

    def osv_vuln(self, call):
        vid = call.path.rsplit("/", 1)[1]
        return (200, self.osv_vulns[vid]) if vid in self.osv_vulns else (404, {"message": "no such advisory"})

    def kev_list(self, call):
        return 200, self.kev

    def hermes_models(self, call):
        return 200, {"data": []}

    def hermes_chat(self, call):
        body = call.body
        self.hermes_calls.append((body, call.auth, call.headers.get("X-Hermes-Session-Id")))
        self.monthly += self.bump          # a run's cost shows up in the counter it returns
        n = int(body["messages"][1]["content"].split("#")[1].split(":")[0])
        if self.hermes == "error":
            return 500, {"error": f"upstream said {ORKEY}"}
        if self.slow():
            time.sleep(2.5)
        if self.hermes in ("report", "slow-report"):   # what the agent does with s9-task
            self.set_labels(n, list(self.labels(n) - {"status:working"}) + ["status:review"])
        reply = {"model": self.hermes_model or body.get("model", "hermes-agent"),
                 "choices": [{"message": {"content": "line 1\nline 2\nall done"}}],
                 "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}
        if self.hermes_runtime:
            reply["runtime"] = self.hermes_runtime
        return 200, reply

    def slow(self):
        return self.hermes in ("slow", "slow-report")

    PREFIXES = ((CREW_GIT, crew_git), ("/ci/", ci), ("/gh/", gh), ("/osv/v1/vulns/", osv_vuln))
    EXACT = {"/or/key": or_key, "/osv/v1/querybatch": osv_query, "/kev/kev.json": kev_list,
             "/hermes/v1/models": hermes_models, CHAT: hermes_chat}

    def handle(self, call):
        """(status, JSON body) for one request."""
        route = self.EXACT.get(call.path)
        if route is None:
            route = next((r for prefix, r in self.PREFIXES if call.path.startswith(prefix)), None)
        if route is None:
            return 404, {}
        return route(self, call)


def frame(payload):
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def chat_frames(out):
    """A chat reply as chat.completion.chunk frames, ending in [DONE]."""
    content = (((out.get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
    frames = []
    if content:
        frames.append(frame({"model": out.get("model", "hermes-agent"), "choices": [{"delta": {"content": content}}]}))
    if out.get("runtime"):
        frames.append(frame({"choices": [{"delta": {}}], "runtime": out["runtime"]}))
    frames.append(frame({"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": out.get("usage", {})}))
    return frames + [b"data: [DONE]\n\n"]


class Handler(BaseHTTPRequestHandler):
    stub = None        # set by serve(), on a subclass

    def any(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        call = Call(self.command, self.path, body, self.headers)
        self.stub.link = None
        streaming = call.path == CHAT and body
        if streaming and body.get("stream"):
            self.sse_chat(call)
            return
        code, out = self.stub.handle(call)
        self.send_json(code, out, self.stub.link)

    def send_json(self, code, out, link=None):
        raw = json.dumps(out).encode()
        self.send_response(code)
        if link:
            self.send_header("Link", link)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def sse_head(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def sse_chat(self, call):
        """Serve a streaming chat completion: an early partial chunk when the stub is slow,
        then the whole reply as chat.completion.chunk frames (so the dispatcher can keep the
        text it saw before its timeout)."""
        slow = self.stub.slow()
        if slow:      # the run has produced something, then stalls past the dispatcher's timeout
            self.sse_head()
            self.wfile.write(frame({"model": "hermes-agent",
                                    "choices": [{"delta": {"content": "partial line 1\n"}}]}))
            self.wfile.flush()
        code, out = self.stub.handle(call)
        if code != 200:
            self.send_json(code, out)
            return
        if not slow:
            self.sse_head()
        try:
            for chunk in chat_frames(out):
                self.wfile.write(chunk)
        except BrokenPipeError:      # the dispatcher already gave up on the run
            pass

    do_GET = do_POST = do_PATCH = any

    def log_message(self, *a):
        pass


def serve(stub):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (Handler,), {"stub": stub}))
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv
