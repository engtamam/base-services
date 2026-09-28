"""Pure 5-field cron evaluator, evaluated in UTC.

    ┌───────── minute        (0-59)
    │ ┌─────── hour          (0-23)
    │ │ ┌───── day-of-month   (1-31)
    │ │ │ ┌─── month          (1-12)
    │ │ │ │ ┌─ day-of-week     (0-6, 0=Sunday)
    * * * * *

Supports `*`, `a-b` ranges, `*/n` and `a-b/n` steps, and `a,b,c` lists. Correctness notes:
  • everything is computed in **UTC** — never local time, so there is no DST ambiguity;
  • when BOTH day-of-month and day-of-week are restricted, cron's real rule is OR (either
    matches), which we implement — a subtle footgun most naive parsers get wrong;
  • `next_after` steps minute-by-minute with a hard bound (4 years) so a never-matching
    expression fails closed instead of looping forever.
"""
from __future__ import annotations

import datetime as _dt

_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("dom", 1, 31), ("month", 1, 12), ("dow", 0, 6))
_MAX_MINUTES = 4 * 366 * 24 * 60


class CronError(ValueError):
    pass


def _parse_field(spec: str, lo: int, hi: int) -> set[int]:
    out: set[int] = set()
    for part in spec.split(","):
        step = 1
        if "/" in part:
            part, _, s = part.partition("/")
            if not s.isdigit() or int(s) < 1:
                raise CronError(f"bad step: {s}")
            step = int(s)
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            if not (a.isdigit() and b.isdigit()):
                raise CronError(f"bad range: {part}")
            start, end = int(a), int(b)
        elif part.isdigit():
            start = end = int(part)
        else:
            raise CronError(f"bad field: {part}")
        if start < lo or end > hi or start > end:
            raise CronError(f"out of range [{lo},{hi}]: {part}")
        out.update(range(start, end + 1, step))
    return out


def parse(expr: str) -> dict[str, set[int]]:
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(f"expected 5 fields, got {len(fields)}: {expr!r}")
    parsed = {}
    for (name, lo, hi), spec in zip(_FIELDS, fields):
        parsed[name] = _parse_field(spec, lo, hi)
    parsed["_dom_restricted"] = fields[2] != "*"
    parsed["_dow_restricted"] = fields[4] != "*"
    _reject_impossible_dom_month(parsed)
    return parsed


# Max day each month can have (February allows 29 for a leap year — a leap Feb 29 is reachable).
_MAX_DAY = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}


def _reject_impossible_dom_month(p: dict) -> None:
    """Reject a syntactically-valid expression whose day-of-month can never occur in any allowed
    month (job-003), e.g. '0 0 30 2 *' (Feb 30) or '0 0 31 2 *' (Feb 31).

    Only when day-of-month is restricted AND day-of-week is NOT: with both restricted cron's rule is
    OR, so an unreachable DOM is harmless (the DOW branch can still fire). With DOW unrestricted the
    match needs a real (day, month) pair; if none exists, next_after would step minute-by-minute to
    the 4-year bound (~2.1M iterations) on the request path before failing.
    """
    if not (p["_dom_restricted"] and not p["_dow_restricted"]):
        return
    if any(day <= _MAX_DAY.get(month, 31) for month in p["month"] for day in p["dom"]):
        return
    raise CronError("day-of-month never occurs in any selected month "
                    f"(dom={sorted(p['dom'])}, month={sorted(p['month'])})")


def _matches(p: dict, t: _dt.datetime) -> bool:
    if t.minute not in p["minute"] or t.hour not in p["hour"] or t.month not in p["month"]:
        return False
    dom_ok = t.day in p["dom"]
    dow_ok = ((t.weekday() + 1) % 7) in p["dow"]           # Python Mon=0 → cron Sun=0
    if p["_dom_restricted"] and p["_dow_restricted"]:
        return dom_ok or dow_ok                             # cron's real OR rule
    return dom_ok and dow_ok


def next_after(expr: str, after: float) -> float:
    """First matching UTC minute strictly after `after` (epoch seconds)."""
    p = parse(expr)
    t = _dt.datetime.fromtimestamp(after, _dt.timezone.utc).replace(second=0, microsecond=0)
    t += _dt.timedelta(minutes=1)
    for _ in range(_MAX_MINUTES):
        if _matches(p, t):
            return t.timestamp()
        t += _dt.timedelta(minutes=1)
    raise CronError(f"expression never matches within 4 years: {expr!r}")


def validate(expr: str) -> None:
    parse(expr)
