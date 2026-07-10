"""Cron evaluator — UTC-correct, DOM/DOW OR rule, fail-closed on impossible/bad exprs."""
from __future__ import annotations

import datetime as dt

import pytest

from app.core.cron import CronError, next_after, validate


def _ts(iso):
    return dt.datetime.fromisoformat(iso).replace(tzinfo=dt.timezone.utc).timestamp()


def _iso(ts):
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M")


@pytest.mark.parametrize("expr,after,expected", [
    ("* * * * *", "2026-01-01T00:00:30", "2026-01-01T00:01"),
    ("*/15 * * * *", "2026-01-01T00:07:00", "2026-01-01T00:15"),
    ("0 0 * * *", "2026-01-01T12:00:00", "2026-01-02T00:00"),
    ("30 9 * * 1-5", "2026-07-10T09:30:00", "2026-07-13T09:30"),   # Fri → Mon (skip weekend)
    ("0 0 1 * *", "2026-01-15T00:00:00", "2026-02-01T00:00"),
    ("0 12 13 * 5", "2026-01-01T00:00:00", "2026-01-02T12:00"),    # DOM=13 OR DOW=Fri → first Fri
    ("0,30 * * * *", "2026-01-01T00:10:00", "2026-01-01T00:30"),
])
def test_next_after(expr, after, expected):
    assert _iso(next_after(expr, _ts(after))) == expected


def test_utc_no_dst_ambiguity():
    # 02:30 daily is unambiguous in UTC across any local DST boundary
    assert _iso(next_after("30 2 * * *", _ts("2026-03-08T00:00:00"))) == "2026-03-08T02:30"


@pytest.mark.parametrize("bad", ["* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
                                 "* * * * 9", "a * * * *", "*/0 * * * *", "5-2 * * * *"])
def test_bad_expressions_rejected(bad):
    with pytest.raises(CronError):
        validate(bad)


def test_impossible_date_fails_closed():
    with pytest.raises(CronError):
        next_after("0 0 30 2 *", _ts("2026-01-01T00:00:00"))    # Feb 30 never exists


def test_next_after_is_strictly_after():
    # exactly on a matching minute → returns the NEXT one, not the same instant
    assert _iso(next_after("0 0 * * *", _ts("2026-01-01T00:00:00"))) == "2026-01-02T00:00"
