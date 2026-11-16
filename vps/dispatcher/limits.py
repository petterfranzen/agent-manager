"""The roster (agents/roster.toml) and the root-only limits file (/etc/section9/limits.toml).

The limits file is outside the repo, so a commit cannot change it: run ceilings, the guardrail
preamble, the model allow-list, per-agent budgets, pacing, the lanes switch and the cash multiplier.
"""

import tomllib
from pathlib import Path

from . import net

DEFAULT_TIMEOUT = 3600       # an agent without timeout_sec in the roster
DEFAULT_RUNS_PER_DAY = 2     # #205 item 6: an agent's daily run cap when the roster names none
PACING_DEFAULTS = {"enabled": True, "slack_days": 0.0}   # #205 item 4: pacing is on unless the file turns it off
DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"   # in step with hermes/config.yaml model.default


def load_roster(path):
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    return data.get("agents", []), data


def limits_of(path):
    """(ceilings, preamble) from the root-only limits file. No file: no ceilings, no preamble."""
    try:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError:
        return {}, ""
    except ValueError as e:
        raise net.Failure(f"cannot read {path}: {e}") from None
    return data.get("timeout_sec", {}), data.get("preamble", "")


def root_file(path):
    """The root-only limits file as a dict. No file or a bad file: {}."""
    try:
        return tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def root_table(path, name):
    """One table from the root-only limits file. No file, no table, or a bad file: {}."""
    table = root_file(path).get(name, {})
    return table if isinstance(table, dict) else {}


def stripped(value):
    """A string without its outer blanks; '' for anything that is not a string."""
    return value.strip() if isinstance(value, str) else ""


def models_of(path):
    """(pins, allow) from the root-only [models] table.

    `pins` maps an agent name to a model id and wins over the roster, so one line rolls that agent
    back to the default. `allow` is every model the dispatcher may send at all: it is what keeps a
    repo change (agents/roster.toml) from picking an arbitrary or xAI model (#101).
    """
    table = root_table(path, "models")
    raw = table.get("allow")
    allow = [stripped(x) for x in raw if stripped(x)] if isinstance(raw, list) else []
    pins = {k: stripped(v) for k, v in table.items() if k != "allow" and stripped(v)}
    return pins, allow


def model_ok(model):
    """Refuse an xAI model. The account's ignore list is repeated here so a bad or edited
    allow-list entry (or a typo) still can never reach Grok."""
    low = (model or "").strip().lower()
    return not (low.startswith(("x-ai", "xai")) or "grok" in low)


def model_for(agent, pins, allow, log=None):
    """The model id a run sends: the root-only pin, else the roster's, else the default.

    A roster value the allow-list does not hold (an unknown id, a typo, an xAI model) is refused
    and the run uses the gateway default, so a repo change can only pick an approved model.
    """
    wanted = pins.get(agent["name"]) or agent.get("model") or DEFAULT_MODEL
    if wanted == DEFAULT_MODEL:
        return wanted
    if wanted in allow and model_ok(wanted):
        return wanted
    if log:
        log(f"{agent['name']}: model {wanted!r} is not in the root allow-list; using {DEFAULT_MODEL}")
    return DEFAULT_MODEL


def spend_caps(path):
    """agent -> USD ceiling from the root-only [spend] table. Absent: no per-agent guard."""
    caps = {}
    for name, value in root_table(path, "spend").items():
        try:
            caps[name] = float(value)
        except (TypeError, ValueError):
            continue
    return caps


def day_cap(agent):
    """The agent's runs-per-day ceiling: the roster's `runs_per_day`, else DEFAULT_RUNS_PER_DAY.
    A missing, unreadable or non-positive value means no day cap."""
    try:
        return int(agent.get("runs_per_day", DEFAULT_RUNS_PER_DAY))
    except (TypeError, ValueError):
        return DEFAULT_RUNS_PER_DAY


def ceiling(agent, caps):
    """The agent's run timeout, never above the root-only ceiling for that agent."""
    want = int(agent.get("timeout_sec", DEFAULT_TIMEOUT))
    cap = caps.get(agent["name"], caps.get("default"))
    return min(want, int(cap)) if cap is not None else want


def pacing_settings(path):
    """The [pacing] table, on by default (#205 item 4); the root-only file only tunes it.

    Absent file, absent table, or a non-table value: PACING_DEFAULTS (enabled, slack_days 0), so
    pacing holds only `priority:low` work when the month is ahead of the line -- no root edit is
    needed to turn it on. A root-only `[pacing] enabled = false` is the one way to opt out.
    """
    return {**PACING_DEFAULTS, **root_table(path, "pacing")}


def lanes_setting(path):
    """The `lanes = 2` switch. Absent, unreadable, or anything but 2: one lane -- the code default
    (#205 item 6 keeps one lane); only a root-only `lanes = 2` in /etc/section9/limits.toml opts in."""
    return 2 if root_file(path).get("lanes", 1) == 2 else 1


def cash_multiplier(path):
    """The root-only `cash_multiplier` (default 1.0): what OpenRouter's usage costs in cash once its credit
    fee and tax are added, for example 1.266 (5.5% credit fee + 20% VAT, measured on an invoice). Absent, unreadable or not a number: 1.0. The
    cap and the fail-closed spend check keep using usage as OpenRouter reports it (the key's own figure)."""
    try:
        return float(root_file(path).get("cash_multiplier", 1.0))
    except (TypeError, ValueError):
        return 1.0
