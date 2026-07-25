"""Resolving a target's internal token from its stored reference.

This lives in CORE, not in the API layer, deliberately. Reading a stored credential's plaintext is
adapter/core work; a route that does it is one `return` away from handing that token back in a
response, and `safety:no-secret-reads` fails the build on exactly that. The route hands this a
secret store and gets back a resolver; the plaintext never surfaces at the boundary.
"""
from __future__ import annotations

from typing import Callable


def make_token_resolver(secret_store) -> Callable:
    """(token_ref) -> token. Empty when nothing is configured — a target requiring auth is then
    simply unreachable until a key and a secret are set, which is a boot decision, never a leak."""
    async def resolve(ref: str) -> str:
        if not ref or secret_store is None:
            return ""
        return await secret_store.get(tenant="", name=ref, fallback="")
    return resolve
