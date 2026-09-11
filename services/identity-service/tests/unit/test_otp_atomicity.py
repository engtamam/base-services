"""Regression for identity-service-03: the OTP attempt counter must be atomic under concurrency.

The old code did a read-modify-write on the JSON record (get -> attempts+=1 -> set). Concurrent wrong
guesses all read the same pre-increment value, so N parallel attempts advanced the counter by 1, not
N — letting a fan-out grind a 6-digit code far past max_attempts within its short life. The memory
adapter hides this (its get/set never suspend), so this test uses a cache whose get/set YIELD between
round-trips (as redis does) while keeping incr() atomic — exactly the shape the fix relies on.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.otp_service import OtpError, OtpService


class _InterleavingCache:
    """get/set/delete suspend (like separate redis round-trips), so concurrent verify() calls
    interleave. incr() is atomic (no suspension between its read and write) — the guarantee the fix
    depends on and that redis INCR provides."""

    driver = "memory"

    def __init__(self) -> None:
        self._data: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        await asyncio.sleep(0)                    # force a scheduling point mid-operation
        return self._data.get(key)

    async def set(self, key: str, value: str, ttl_seconds=None) -> None:
        await asyncio.sleep(0)
        self._data[key] = value

    async def incr(self, key: str, ttl_seconds=None) -> int:
        nxt = int(self._data.get(key, "0")) + 1   # atomic: no await between read and write
        self._data[key] = str(nxt)
        return nxt

    async def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    async def health(self) -> bool:
        return True

    async def close(self) -> None:
        self._data.clear()


def test_concurrent_wrong_guesses_each_burn_an_attempt():
    async def _run():
        cache = _InterleavingCache()
        svc = OtpService(cache=cache, salt="pepper", max_attempts=5)
        ident = "race@test.com"
        await svc.issue(identifier=ident)
        # Fire 20 concurrent WRONG guesses against a code with a ceiling of 5.
        results = await asyncio.gather(
            *[svc.verify(identifier=ident, code="000000") for _ in range(20)],
            return_exceptions=True)
        codes = [getattr(r, "code", None) for r in results]
        invalid = codes.count("INVALID")
        refused = codes.count("TOO_MANY_ATTEMPTS") + codes.count("NO_CODE")
        # THE security property: at most max_attempts guesses reach the hash compare (INVALID); every
        # other concurrent guess is refused (over the ceiling, or the code already destroyed). The
        # pre-fix read-modify-write collapsed the counter so nearly all 20 returned INVALID.
        assert invalid <= 5, f"ceiling breached: {invalid} guesses reached the compare"
        assert invalid + refused == 20
        # and the code itself was destroyed once exhausted
        with pytest.raises(OtpError) as e:
            await svc.verify(identifier=ident, code="000000")
        assert e.value.code in ("NO_CODE", "TOO_MANY_ATTEMPTS")

    asyncio.run(_run())
