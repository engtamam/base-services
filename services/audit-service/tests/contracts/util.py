"""Helper to drive async adapters from sync tests without an asyncio plugin.

ONE event loop for the whole session, deliberately.

`asyncio.run()` creates and destroys a loop per call. That is fine for a stateless in-memory
adapter, but a real connection pool binds its sockets to the loop that created it: the next
`run()` gets a new loop, and every query fails with "Event loop is closed". A pooled adapter
could therefore never be exercised across two calls — which is exactly how the postgres
contract went unverified.
"""
from __future__ import annotations

import asyncio
from typing import Any

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run(coro: Any) -> Any:
    return _get_loop().run_until_complete(coro)
