"""Routines: the roster's (and the root-only private file's) cron entries, opened as issues when due."""

import datetime as dt
import json
import tomllib
import urllib.parse
from pathlib import Path
from zoneinfo import ZoneInfo

from . import limits
from .when import utc_now


def part_matches(part, value, bounds):
    """One comma-separated part of a cron field: `*`, `a`, `a-b`, each with an optional `/step`."""
    rng, _, step = part.partition("/")
    first, _, last = rng.partition("-")
    start, end = bounds if rng == "*" else (int(first), int(last or first))
    in_range = start <= value <= end
    return in_range and (value - start) % int(step or 1) == 0


def field_matches(spec, value, lo, hi):
    return any(part_matches(part, value, (lo, hi)) for part in spec.split(","))


def cron_matches(cron, t):
    m, h, dom, mon, dow = cron.split()
    fields = ((m, t.minute, 0, 59), (h, t.hour, 0, 23), (dom, t.day, 1, 31), (mon, t.month, 1, 12),
              (dow, (t.weekday() + 1) % 7, 0, 6))   # 0 = Sunday
    return all(field_matches(*field) for field in fields)


def last_fire(cron, tz, since, now):
    """Latest minute in (since, now] matching cron in tz, else None."""
    t = now.astimezone(tz).replace(second=0, microsecond=0)
    limit = since.astimezone(tz)
    for _ in range(3 * 24 * 60):          # catch up at most 3 days after downtime
        if t <= limit:
            return None
        if cron_matches(cron, t):
            return t
        t -= dt.timedelta(minutes=1)
    return None


def load_error(e):
    """What may be logged about a private file that did not load: the position of the error
    (tomllib's message has line and column, no text), never the file's contents."""
    if isinstance(e, OSError):
        return e.strerror
    return str(e) if isinstance(e, tomllib.TOMLDecodeError) else type(e).__name__


def private_entries(path, log):
    """The [[routines]] tables of the private file; [] (logged) when it cannot be read or parsed."""
    try:
        entries = tomllib.loads(path.read_text(encoding="utf-8")).get("routines", [])
        if not isinstance(entries, list):
            raise ValueError("routines is not an array of tables")
    except (OSError, ValueError) as e:      # TOMLDecodeError and UnicodeDecodeError are ValueErrors
        log(f"routines: error: private routines not loaded ({type(e).__name__}: {load_error(e)}); public routines continue")
        return []
    return entries


def wrong_fields(r):
    """The fields of a private routine that are missing or not text."""
    if not isinstance(r, dict):
        return ["agent", "title", "cron"]
    bad = [k for k in ("agent", "title", "cron") if not isinstance(r.get(k), str)]
    if not bad and not isinstance(r.get("timezone", "UTC"), str):
        return ["timezone"]
    return bad


def schedule_ok(r):
    """True when the routine's timezone exists and its cron has five fields."""
    try:
        ZoneInfo(r.get("timezone", "UTC"))
    except (ValueError, KeyError, OSError):      # ZoneInfoNotFoundError is a KeyError
        return False
    return len(r["cron"].split()) == 5


def routine_problems(r):
    bad = wrong_fields(r)
    if not bad and not schedule_ok(r):
        return ["cron or timezone"]
    return bad


def private_routines(path, log):
    """Root-only extras kept out of the public repo: [[routines]] agent/title/description/cron/timezone.

    A missing file is normal. A file that cannot be read or parsed, or an entry without its fields,
    is logged and skipped so the public routines still run.
    """
    if not path.exists():
        return []
    items = []
    for n, r in enumerate(private_entries(path, log), 1):
        bad = routine_problems(r)
        if bad:
            log(f"routines: error: private routine {n} skipped (check: {', '.join(bad)})")
            continue
        items.append((r["agent"], {**r, "active": True}))
    return items


def still_pending(board, label, title):
    """The open issues of this routine that have not run yet (todo) or are running.

    Only those block a new one. An older one that is waiting in review (its PR is still being QA'd),
    blocked or forgotten must NOT stop tonight's run: "one build a night" has to happen every
    night (Petter, 6 Nov)."""
    open_ = board.pages(f"/issues?labels={urllib.parse.quote(label)}&state=open")
    return [i for i in open_ if i["title"] == title
            and {"status:todo", "status:working"} & {l["name"] for l in i["labels"]}]


def open_routine(ctx, name, r, fired):
    """Open the routine's issue, unless the last one is still waiting or running."""
    label = f"agent:{name.lower()}"
    pending = still_pending(ctx.board, label, r["title"])
    if pending:
        ctx.log(f"{name}: '{r['title']}' (#{pending[0]['number']}) is still waiting or running; skipped")
        return
    labels = [label, "status:todo"]
    if r.get("priority") in ("high", "low"):      # a routine can claim a priority: the nightly build is high
        labels.append(f"priority:{r['priority']}")
    ctx.board.call("POST", "/issues", {"title": r["title"], "body": r.get("description", ""), "labels": labels})
    ctx.log(f"{name}: opened '{r['title']}' ({fired:%Y-%m-%d %H:%M %Z})")


def last_run(state_path, now):
    """When routines last ran; `now` on a first run, so nothing is backfilled."""
    try:
        return dt.datetime.fromisoformat(json.loads(state_path.read_text())["at"])
    except (OSError, ValueError, KeyError):
        return now


def routines(ctx):
    a = ctx.a
    agents, _ = limits.load_roster(a.roster)
    items = [(x["name"], x["routine"]) for x in agents if x.get("routine", {}).get("active")]
    items += private_routines(Path(a.private_routines), ctx.log)
    now = utc_now()
    state_path = Path(a.state_dir) / "routines.json"
    since = last_run(state_path, now)
    for name, r in items:
        fired = last_fire(r["cron"], ZoneInfo(r.get("timezone", "UTC")), since, now)
        if fired:
            open_routine(ctx, name, r, fired)
    Path(a.state_dir).mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"at": now.isoformat()}))
