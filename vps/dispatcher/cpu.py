"""This VPS's own CPU figures, from /proc/stat: the run ledger's samples and the "CPU credits" issue.

Hostinger gives a VPS 25% of a vCPU for free and a balance of credits above that; when the balance
is spent it limits the VPS, which shows up as steal. No Hostinger API key is used or needed.
"""

import json
import os
import time
from pathlib import Path

from . import board, net
from .when import utc_now

SAMPLE_SECONDS = 5.0         # window for the /proc/stat sample behind the CPU credits snapshot
PROBE_SECONDS = 1.0          # length of the throttling probe; ~18 credits a day at this size
BASELINE = 0.25              # share of one vCPU a Hostinger VPS uses without spending credits
BALANCE_MAX = 8100.0         # core-seconds in a full balance: 3 h x 3600 x (1 vCPU - 25%)
SAFE_CREDITS_DAY = 8100.0    # one full balance a day is the guidance the Capacity issue measures against
CLK_TCK = float(os.sysconf("SC_CLK_TCK"))   # /proc/stat counts in USER_HZ ticks, not seconds
SANDBOX_CORES, HERMES_CORES = 0.4, 0.3    # keep in step with vps/compose.yaml
IDLE, IOWAIT, STEAL = 3, 4, 7             # columns of the cpu line


def cpu_fields():
    """The first ``cpu`` line of /proc/stat as ints: user nice system idle iowait irq softirq steal."""
    for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
        if line.startswith("cpu "):
            return [int(v) for v in line.split()[1:]]
    raise net.Failure("/proc/stat has no cpu line")


def busy_seconds(fields):
    """Core-seconds this VPS actually used in a ``cpu`` line (idle, iowait and steal are not ours).

    /proc/stat counts USER_HZ ticks (normally 100 a second), not seconds. Treating them as
    seconds made the hourly draw read ~100x too high (section9-hq #44: 117,609 credits/hour).
    """
    return float(sum(fields[:3]) + sum(fields[5:7])) / CLK_TCK


def cpu_sample():
    """A cumulative CPU snapshot, in seconds, for the run ledger."""
    fields = cpu_fields()
    return {"at": time.time(), "busy": busy_seconds(fields),
            "steal": fields[STEAL] / CLK_TCK, "total": sum(fields) / CLK_TCK}


def cpu_delta(before, after):
    """What one run cost in CPU: steal share, core-seconds and credits above the baseline.

    Credits are core-seconds above Hostinger's free 25% baseline (section9-hq #34). This is
    an estimate from /proc/stat: it includes everything on the VPS, not only the run, so a
    single lane makes it attributable; two lanes do not.
    """
    wall = max(after["at"] - before["at"], 0.0)
    total = max(after["total"] - before["total"], 0.0)
    busy = max(after["busy"] - before["busy"], 0.0)
    steal = max(after["steal"] - before["steal"], 0.0)
    return {"wall_seconds": round(wall, 3),
            "steal_pct": round(100.0 * steal / total, 2) if total else 0.0,
            "core_seconds": round(busy, 3),
            "credits": round(max(0.0, busy - BASELINE * wall), 3)}


def steal_secs(seconds):
    """CPU steal over `seconds` of wall clock. Two /proc/stat reads: it spends no CPU of its own."""
    first = cpu_sample()
    time.sleep(max(float(seconds), 0.0))
    return cpu_delta(first, cpu_sample())["steal_pct"]


def cpu_ceiling():
    """CPU one thread gets per second of wall clock: ~1.0 uncapped, ~0.25 while Hostinger's
    limit is on. One second of load, so the snapshot costs ~18 credits a day at worst."""
    started = time.process_time()
    until = time.monotonic() + PROBE_SECONDS
    while time.monotonic() < until:
        pass
    return time.process_time() - started


def shares(first, second):
    """A function from a cpu-line column to its percent of the time between two `cpu_fields` reads;
    0.0 for a column this kernel does not report."""
    total = float(sum(y - x for x, y in zip(first, second))) or 1.0

    def share(i):
        return 100.0 * (second[i] - first[i]) / total if i < len(second) else 0.0
    return share


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def hourly_draw(state_dir, fields, now):
    """Credits an hour drawn since the previous snapshot, or None without one. Writes this snapshot."""
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    state = Path(state_dir) / "cpu-credit.json"
    before = read_json(state)
    if before.get("v") != 2:          # the first, tick-as-seconds snapshot: its busy value is apples to oranges
        before = {}
    draw = None
    if before.get("at") and before.get("busy") is not None:
        hours = max((now.timestamp() - float(before["at"])) / 3600.0, 0.01)
        draw = (busy_seconds(fields) - float(before["busy"]) - BASELINE * hours * 3600.0) / hours
    state.write_text(json.dumps({"v": 2, "at": now.timestamp(), "busy": busy_seconds(fields)}), encoding="utf-8")
    return draw


def pressure():
    """The 60 s `some` average of /proc/pressure/cpu, as the kernel writes it, or 'n/a'."""
    try:
        some = next(l for l in Path("/proc/pressure/cpu").read_text(encoding="utf-8").splitlines()
                    if l.startswith("some"))
        return next((f for f in some.split() if f.startswith("avg60=")), "n/a")
    except (OSError, StopIteration):
        return "n/a"


def limit_on(ceiling, steal):
    """A short ceiling alone can be our own agents' load; Hostinger's limit shows as steal too."""
    return ceiling < 0.9 and steal > 10


def credit_lines(now, share, ceiling, draw):
    """The body of the "CPU credits" issue."""
    lines = [
        f"**Live snapshot, updated hourly by the VPS.** As of {now:%Y-%m-%d %H:%M} UTC.", "",
        f"- **Limit: {'ON' if limit_on(ceiling, share(STEAL)) else 'off'}** — one thread gets {ceiling:.2f} of a core.",
        f"- **Steal: {share(STEAL):.1f}%** of the last {SAMPLE_SECONDS:.0f} s"
        " (Hostinger took that time; the temporary limit is what does it).",
        f"- Busy: {100.0 - share(IDLE) - share(IOWAIT) - share(STEAL):.1f}% of one vCPU, "
        f"iowait {share(IOWAIT):.1f}%, PSI cpu {pressure()}.",
    ]
    if draw is not None:
        lines.append(f"- **Draw: ~{draw:.0f} credits/hour** ({100.0 * draw / BALANCE_MAX:.0f}% of the "
                     f"{BALANCE_MAX:.0f}-credit balance an hour) since the previous snapshot.")
    rate = 3600.0 * (SANDBOX_CORES + HERMES_CORES - BASELINE)
    return lines + [
        "",
        f"Caps in force: sandbox {SANDBOX_CORES} + hermes {HERMES_CORES} = "
        f"{SANDBOX_CORES + HERMES_CORES:.2f} of one vCPU = {rate:.0f} credits/hour, so a full "
        f"balance is {BALANCE_MAX / rate:.1f} h at the cap.",
        "", "Rewritten every hour; don't edit. Leave this issue open."]


def credits(ctx):
    """Keep one open issue titled "CPU credits" current: steal, the measured ceiling and the draw
    on Hostinger's balance. Read from this VPS only; no Hostinger API key is used or needed."""
    first = cpu_fields()
    time.sleep(SAMPLE_SECONDS)
    second = cpu_fields()
    ceiling = cpu_ceiling()
    now = utc_now()
    draw = hourly_draw(ctx.a.state_dir, second, now)
    body = "\n".join(credit_lines(now, shares(first, second), ceiling, draw))
    board.keep_issue(ctx.board, ctx.log, "CPU credits", body)
