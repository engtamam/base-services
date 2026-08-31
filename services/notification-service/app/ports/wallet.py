"""WalletClient port — the reserve/confirm/release primitives the send-time gate calls on wallet-service.

Reserve-then-settle: hold the overage before the send, then confirm the hold if the send actually went
out on a provider-billed driver, or release it otherwise. wallet-service's holds auto-expire, so a
confirm/release we fail to deliver strands nothing — the money simply frees on expiry, which errs
toward under-charging, never over-charging.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

# Why a reservation did not yield a usable hold. "" == success (hold_id is set).
INSUFFICIENT = "insufficient"   # 402 — the wallet cannot cover the overage; the send is refused
UNAVAILABLE = "unavailable"     # anything else (unreachable, contention, bad response) — posture decides


@dataclass(frozen=True)
class ReserveResult:
    hold_id: str = ""
    refused: str = ""           # "" on success, else INSUFFICIENT or UNAVAILABLE

    @property
    def ok(self) -> bool:
        return bool(self.hold_id) and not self.refused


@runtime_checkable
class WalletClient(Protocol):
    async def reserve(self, *, tenant: str, amount: int, source: str, reason: str) -> ReserveResult:
        """Hold `amount` minor units (halalas) for an in-flight send. Never raises for a money refusal —
        an insufficient balance comes back as ReserveResult(refused=INSUFFICIENT)."""
        ...

    async def confirm(self, *, hold_id: str) -> bool:
        """The send went out on a provider-billed driver: consume the hold. Idempotent."""
        ...

    async def release(self, *, hold_id: str) -> bool:
        """The send failed or fell over to a non-billed driver: free the hold. Idempotent."""
        ...
