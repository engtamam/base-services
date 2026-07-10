"""Builtin cron parser — wraps the pure UTC evaluator in core/cron.py."""
from __future__ import annotations

from app.core import cron as _cron


class BuiltinCron:
    driver = "builtin"

    def next_after(self, expr: str, after: float) -> float:
        return _cron.next_after(expr, after)

    def validate(self, expr: str) -> None:
        _cron.validate(expr)
