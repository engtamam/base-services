"""No wallet wired — the default.

An adopter who does not run wallet-service must not acquire a background task that fails for ever
against a service they never deployed. Recording still happens, so switching the driver on later
delivers everything that accumulated rather than starting from empty.
"""
from __future__ import annotations


class NullWallet:
    driver = "null"

    async def credit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                     reason: str = "", expires_at: float | None = None) -> bool:
        return False

    async def debit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                    reason: str = "", allow_negative: bool = False) -> bool:
        return False

    async def health(self) -> bool:
        return True
