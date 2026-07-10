"""Null orchestrator — records the task but dispatches nothing.

Returns False: the task was NOT sent, so the request can never be marked complete on the
strength of a dispatch that never happened. Honest by construction.
"""
from __future__ import annotations


class NullOrchestrator:
    driver = "null"

    async def dispatch(self, **kwargs) -> bool:
        return False
