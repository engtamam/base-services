"""Manual clock — advanceable, for deterministic tests of lease/cron timing."""
from __future__ import annotations


class ManualClock:
    driver = "manual"

    def __init__(self, start: float = 0.0) -> None:
        self._t = float(start)

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += float(seconds)

    def set(self, t: float) -> None:
        self._t = float(t)
