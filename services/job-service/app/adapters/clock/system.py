"""System clock — real wall-clock UTC epoch seconds."""
from __future__ import annotations

import time


class SystemClock:
    driver = "system"

    def now(self) -> float:
        return time.time()
