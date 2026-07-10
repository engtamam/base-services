"""Helper to drive async adapters from sync tests without an asyncio plugin."""
from __future__ import annotations

import asyncio
from typing import Any


def run(coro: Any) -> Any:
    return asyncio.run(coro)
