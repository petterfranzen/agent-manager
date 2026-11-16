"""OpenRouter's own figures for the key the agents use: the cap, what is left, and the pace line."""

import calendar
from decimal import Decimal

from . import net
from .when import month_start

RESERVE = Decimal("1.00")   # stop this far below OpenRouter's cap (the company budget is $19 of $20)
KEY_EMPTY = ("OpenRouter's /key reply was empty or not what was expected; refusing to run until it "
             "reports the key's cap.")
KEY_NO_CAP = ("The OpenRouter key has no monthly cap (limit is null); refusing to run. Put a cap on "
              "the key, then set `status:todo` again.")
KEY_NO_BALANCE = ("OpenRouter's /key reply has a cap but reports neither a remaining balance nor usage; "
                  "refusing to run until it reports what has been spent.")
KEY_NOT_A_NUMBER = "OpenRouter's /key reply has a cap or balance that is not a number; refusing to run."


def is_number(value):
    try:
        Decimal(str(value))
    except ArithmeticError:
        return False
    return True


def key_problem(k):
    """Why a /key reply cannot be used to admit a run, or ''."""
    if not isinstance(k, dict) or not k:
        return KEY_EMPTY
    if k.get("limit") is None:
        return KEY_NO_CAP
    silent = k.get("limit_remaining") is None and k.get("usage") is None
    if silent and is_number(k["limit"]):      # a cap that is not a number is reported by spend_ok
        return KEY_NO_BALANCE
    return ""


def key_left(k):
    """What the key may still spend: its remaining balance, else cap minus usage. ArithmeticError
    when a figure is not a number."""
    if k.get("limit_remaining") is not None:
        return Decimal(str(k["limit_remaining"]))
    return Decimal(str(k["limit"])) - Decimal(str(k.get("usage", 0)))


def spend_ok(openrouter, key):
    """Fails closed: no cap, or a reply that cannot be read, means no run."""
    reply = net.http("GET", openrouter.rstrip("/") + "/key", key, timeout=20)
    k = reply.get("data") if isinstance(reply, dict) else None
    problem = key_problem(k)
    if problem:
        return False, problem
    try:
        left = key_left(k)
    except ArithmeticError:
        return False, KEY_NOT_A_NUMBER
    if left <= RESERVE:
        return False, (f"OpenRouter cap reached: ${left:.2f} left of ${k['limit']} (reserve ${RESERVE}). "
                       f"Raise the cap or wait for the reset, then set `status:todo` again.")
    return True, ""


def key_usage(openrouter, key):
    """OpenRouter's counters, best effort: a ledger must never stop a run or a report.

    The 10 s timeout bounds a slow read. One run reads it twice, before it starts and after it
    ends, so a slow OpenRouter can delay a run's start by up to 10 s (docs/CAPACITY.md).
    """
    try:
        reply = net.http("GET", openrouter.rstrip("/") + "/key", key, timeout=10)
    except Exception:
        return {}
    data = reply.get("data") if isinstance(reply, dict) else None
    if not isinstance(data, dict):
        return {}
    return {name: data.get(name) for name in
            ("usage", "usage_monthly", "usage_daily", "limit", "limit_remaining")}


def month_pace(now, cap, spend, reserve=RESERVE):
    """The two pace lines: fixed linear for the month, and what is left over the days left."""
    days = calendar.monthrange(now.year, now.month)[1]
    elapsed = max((now - month_start(now)).total_seconds() / 86400.0, 0.0)
    linear = max(float(cap) - float(reserve), 0.0) / days
    left_days = max(days - elapsed, 0.0)
    remaining = float(cap) - float(spend) - float(reserve)
    return {"days": days, "elapsed": elapsed, "left_days": left_days,
            "linear_per_day": linear, "linear_to_date": linear * elapsed,
            "remaining_per_day": (remaining / left_days) if left_days > 0 else 0.0}


def ahead_of(pace, spend, slack_days):
    """True when `spend` is more than `slack_days` of linear pace ahead of a `month_pace` line."""
    return float(spend) > pace["linear_to_date"] + pace["linear_per_day"] * slack_days


def over_pace(now, cap, spend, slack_days=1.0):
    """True when month-to-date spend is more than `slack_days` ahead of the linear pace."""
    return ahead_of(month_pace(now, cap, spend), spend, slack_days)
