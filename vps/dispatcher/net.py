"""Secrets, redaction and the HTTP calls. Secrets are never printed."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

KEYS = re.compile(r"(sk-or-|github_pat_|ghp_)[A-Za-z0-9_-]*")
LOOPBACK = {"127.0.0.1", "localhost", "::1"}


class Failure(Exception):
    """A request that failed. `partial` carries the text a streamed reply had already sent."""

    def __init__(self, message, partial=""):
        super().__init__(message)
        self.partial = partial


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


def last_value(text, name):
    """The value of the last NAME=VALUE line for `name`, or None."""
    pairs = [env_pair(line) for line in text.splitlines()]
    values = [pair[1] for pair in pairs if pair and pair[0] == name]
    return values[-1] if values else None


def read_env_value(path, name):
    """The last NAME=VALUE line for `name` in an env file. Missing or empty is a Failure."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise Failure(f"cannot read {path}: {e.strerror}") from None
    value = last_value(text, name)
    if not value:
        raise Failure(f"{name} is missing or empty in {path}")
    return value


def new_request(method, url, token, accept="application/json"):
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Accept", accept)
    req.add_header("User-Agent", "section9-dispatch")
    return req


def send_json(req, body):
    """Put a JSON body on a request."""
    req.data = json.dumps(body).encode()
    req.add_header("Content-Type", "application/json")


def failure_of(error, what, timed_out):
    """The Failure message for an urllib error; `timed_out` is the wording for a timeout."""
    if isinstance(error, urllib.error.HTTPError):
        return f"{what} -> {error.code}: {redact(error.read().decode('utf-8', 'replace'))[:300]}"
    if isinstance(error, TimeoutError):
        return f"{what} -> {timed_out}"
    return f"{what} -> no response: {redact(str(getattr(error, 'reason', error)))}"


def request(method, url, token, **options):
    """(parsed JSON or None, response headers). Options: `body`, sent as JSON; `timeout`, 30 s unless given."""
    timeout = options.get("timeout", 30)
    req = new_request(method, url, token)
    if options.get("body") is not None:
        send_json(req, options["body"])
    what = f"{method} {redact(url)}"
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw, resp_headers = resp.read(), resp.headers
    except OSError as e:      # HTTPError, URLError and TimeoutError are all OSErrors
        raise Failure(failure_of(e, what, f"timed out after {timeout} s")) from None
    try:
        return (json.loads(raw) if raw.strip() else None), resp_headers
    except ValueError:
        raise Failure(f"{what} -> reply is not JSON") from None


def http(method, url, token, **options):
    """The parsed JSON of `request` (same options)."""
    return request(method, url, token, **options)[0]


class Stream:
    """A streamed chat reply as it arrives: the text so far and what the last events said."""

    def __init__(self):
        self.text, self.finish = [], None
        self.model, self.usage, self.runtime = None, None, None

    def said(self):
        return "".join(self.text)

    def take(self, event):
        choice = (event.get("choices") or [{}])[0] or {}
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            self.text.append(delta["content"])
        # `runtime` is the served pair. Absent from the finish chunk, the reply keeps a `model`, and
        # served_model() books the ledger to that (else what we asked for): no #101 gap.
        for name, kind in (("model", str), ("usage", dict), ("runtime", dict)):
            if isinstance(event.get(name), kind):
                setattr(self, name, event[name])
        if choice.get("finish_reason"):
            self.finish = choice["finish_reason"]

    def reply(self):
        """The same shape as a non-streaming reply, so the caller does not change."""
        return {"choices": [{"message": {"content": self.said()}, "finish_reason": self.finish or "stop"}],
                "model": self.model, "usage": self.usage, "runtime": self.runtime}


def sse_events(resp):
    """The JSON events of a Server-Sent Events body, up to `[DONE]`. Comments, `event:` lines,
    blank lines and anything that is not JSON are skipped."""
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        chunk = line[5:].strip() if line.startswith("data:") else None
        if chunk == "[DONE]":
            return
        event = parsed(chunk)
        if event is not NOT_JSON:
            yield event


NOT_JSON = object()


def parsed(chunk):
    """The chunk as JSON; NOT_JSON for no chunk or one that does not parse."""
    if chunk is None:
        return NOT_JSON
    try:
        return json.loads(chunk)
    except ValueError:
        return NOT_JSON


def chat_request(url, token, body, headers=None):
    """The streaming POST for a chat completion: `stream: true` turns the reply into Server-Sent Events."""
    req = new_request("POST", url, token, accept="text/event-stream")
    send_json(req, dict(body, stream=True))
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    return req


def read_stream(resp, stream, clock):
    """Feed every event into `stream`; a Failure once the clock is past its deadline."""
    what, start, deadline = clock
    for event in sse_events(resp):
        stream.take(event)
        if time.monotonic() >= deadline:
            raise Failure(f"{what} -> timed out at {time.monotonic() - start:.0f} s", stream.said())


def chat_stream(req, timeout):
    """Send a `chat_request` and read the answer as it arrives. Returns (reply, partial).

    Text is kept as it arrives, so a run cut off at its timeout still leaves the answer it
    produced so far; a `Failure` from a timeout carries that text in `.partial`. A gateway
    that answers with plain JSON (no streaming) is read as the ordinary reply.
    """
    what = f"POST {redact(req.full_url)}"
    stream = Stream()
    start = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if "text/event-stream" not in resp.headers.get("Content-Type", ""):
                raw = resp.read()
                return (json.loads(raw) if raw.strip() else None), ""
            read_stream(resp, stream, (what, start, start + timeout))
    except OSError as e:      # HTTPError, URLError and TimeoutError are all OSErrors
        timed_out = f"timed out at {time.monotonic() - start:.0f} s"
        raise Failure(failure_of(e, what, timed_out), stream.said()) from None
    return stream.reply(), stream.said()


def next_link(headers):
    """The URL of Link: rel="next", or None."""
    for part in (headers.get("Link") or "").split(","):
        m = re.match(r'\s*<([^>]+)>\s*;.*\brel="?next"?', part)
        if m:
            return m.group(1)
    return None


def local_only(url, what):
    u = urllib.parse.urlsplit(url)
    if u.scheme != "https" and u.hostname not in LOOPBACK:
        raise Failure(f"refusing to send {what} over plain HTTP to {u.hostname}")
