"""The optional second lane (#79): its lock files, the root-only switch and the fallback latch."""

import fcntl
import json
from pathlib import Path

from . import cpu, ledger, limits, net
from .when import parse_when, utc_now

LANE_STATE = "lanes.json"         # the lane fallback latch, under --state-dir
PICK_LOCK = "dispatch.pick.lock"  # one lane chooses at a time; held only while choosing
LANE_LOCK = "dispatch.lane{}.lock"  # one per lane, held for the whole run (#79)
DEFAULT_QUIET_HOURS = 3.0        # safe readings this long before the second lane comes back
SAFE_STEAL_PCT = 10.0            # above this Hostinger's limit is on (the [pacing] default)


def try_lock(f):
    """Take a file's lock without waiting. False when another process holds it."""
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def lane_lock_held(path):
    """True while a run holds a lane's lock file (opened "w" and flock'd for the whole run).

    A `lanes` flip from 2 to 1 must not drop the exclusion while a lane-1 run is still in flight: the run
    outlives the tick that started it. Best effort: a path that cannot be opened counts as not held."""
    try:
        f = open(path, "w")
    except OSError:
        return False
    with f:
        if not try_lock(f):
            return True
        fcntl.flock(f, fcntl.LOCK_UN)
    return False


def lane_fallback(a, rows, now):
    """Why a second lane must not run right now, or ''. Steal first (Hostinger's temporary limit
    shows there, as the hourly CPU credits issue reads it), then the runs' own credit draw."""
    setting = limits.pacing_settings(a.limits)
    try:
        steal = cpu.steal_secs(setting.get("sample_seconds", 1.0))
    except (net.Failure, OSError, IndexError, KeyError, ValueError) as e:
        return f"cpu unreadable ({e})"
    limit = float(setting.get("steal_pct", SAFE_STEAL_PCT))
    if steal > limit:
        return f"CPU steal {steal:.1f}% is over {limit:.0f}%: Hostinger's limit is on"
    draw = ledger.ledger_credits(rows, now)
    if draw >= cpu.SAFE_CREDITS_DAY:
        return f"the last 24 h drew {draw:.0f} CPU credits, over the {cpu.SAFE_CREDITS_DAY:.0f}/day guidance"
    return ""


def write_lane_state(path, state):
    """The latch is advice for the next tick: a failed write is logged, never fatal."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    except OSError as e:
        print(f"lanes: cannot write {path}: {e}", flush=True)


def read_lane_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def quiet_period(a, state, now):
    """Why the lane still waits after an unsafe reading, or '' once `quiet_hours` have passed."""
    unsafe_at = parse_when(state.get("unsafe_at"))
    quiet = float(limits.pacing_settings(a.limits).get("quiet_hours", DEFAULT_QUIET_HOURS))
    if not unsafe_at:
        return ""
    left = (quiet * 3600.0 - (now - unsafe_at).total_seconds()) / 60.0
    if left <= 0:
        return ""
    return f"quiet period: {left:.0f} min left of {quiet:.0f} h after an unsafe reading"


def lane_state(a, log, now=None):
    """(lanes allowed, why). The root-only switch, the fallback latch and the quiet period.

    Two lanes only while the switch says 2, the reading is safe, and `quiet_hours` have passed
    since the last unsafe one. An unsafe reading drops to one lane and writes the latch; the lane
    returns only after that quiet period, so a single steal spike cannot flap it (#79)."""
    now = now or utc_now()
    if limits.lanes_setting(a.limits) < 2:
        return 1, "the lanes switch is 1"
    path = Path(a.state_dir) / LANE_STATE
    state = read_lane_state(path)
    stamp = now.isoformat(timespec="seconds")
    reason = lane_fallback(a, ledger.read_ledger(ledger.ledger_path(a)), now)
    if reason:
        write_lane_state(path, {"lanes": 1, "at": stamp, "unsafe_at": stamp, "reason": reason})
        log(f"lanes: down to one lane: {reason}")
        return 1, reason
    waiting = quiet_period(a, state, now)
    if waiting:
        return 1, waiting
    if state.get("lanes") != 2:
        write_lane_state(path, {"lanes": 2, "at": stamp, "unsafe_at": state.get("unsafe_at"), "reason": ""})
        log("lanes: back to two lanes")
    return 2, ""
