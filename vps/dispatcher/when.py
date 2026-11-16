"""Times: everything the dispatcher records or compares is UTC."""

import datetime as dt


def utc_now():
    return dt.datetime.now(dt.timezone.utc)


def parse_when(value):
    """An ISO timestamp as a datetime, or None when it cannot be read."""
    try:
        return dt.datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def month_start(now):
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def day_start(now):
    return now.replace(hour=0, minute=0, second=0, microsecond=0)
