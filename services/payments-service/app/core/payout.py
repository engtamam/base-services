"""Payout tracking — the provider pays sub-merchants out on its settlement cycle; we OBSERVE the outcome.

A Payout is our record of "the provider reported settlement X to a sub-merchant, and here is its status".
We never initiate a payout (the provider does, from the funds it holds); we learn about it from a payout
webhook and, as a safety net, by re-querying the provider for any payout we last saw PENDING — the exact
discipline the charge reconcile uses.

Crash/replay safety, mirroring PaymentRequestStore:
  * the row is CREATED via cas insert-if-absent, so it always carries a version and can be transitioned;
  * the terminal transition (pending -> paid | failed) is a version-pinned CAS, so a webhook and a
    reconcile racing on the same payout yield exactly ONE transition (and one payout.settled/failed event);
  * a redelivered terminal webhook is the idempotent no-op; a terminal row is never overwritten.
"""
from __future__ import annotations

import time

from app.ports.payment_gateway import PAYOUT_FAILED, PAYOUT_PAID, PAYOUT_PENDING

_COLL = "payouts"
_TERMINAL = (PAYOUT_PAID, PAYOUT_FAILED)


def _now() -> float:
    return time.time()


class PayoutStore:
    def __init__(self, store, *, now=_now) -> None:
        self._s = store
        self._now = now

    async def get(self, payout_ref: str) -> dict | None:
        return await self._s.get(_COLL, payout_ref)

    async def apply(self, *, payout_ref: str, status: str, provider: str = "", amount_minor: int = 0,
                    currency: str = "", destination_id: str = "", tenant: str = "",
                    event_id: str = "") -> tuple[bool, dict]:
        """Observe a payout and move it to its terminal state AT MOST ONCE. Returns (transitioned_now, row):
        transitioned_now=True exactly when this call takes the payout to paid/failed (the caller then emits
        the event). A first observation that is already terminal counts as the transition; a pending
        observation records the row without transitioning; a replay of a terminal state is a no-op."""
        if not payout_ref:
            raise ValueError("payout_ref is required")
        status = (status or PAYOUT_PENDING).lower()
        existing = await self.get(payout_ref)
        if existing is None:
            now = self._now()
            row = {"id": payout_ref, "payout_ref": payout_ref, "status": status, "provider": provider,
                   "amount_minor": int(amount_minor), "currency": (currency or "").upper(),
                   "destination_id": destination_id, "tenant": tenant,
                   "applied_event_id": event_id if status in _TERMINAL else "",
                   "created_at": now, "updated_at": now}
            if not await self._s.cas(_COLL, payout_ref, 0, row):   # insert-if-absent
                # Lost the create race — someone else just observed it; re-read and re-apply idempotently.
                return await self.apply(payout_ref=payout_ref, status=status, provider=provider,
                                        amount_minor=amount_minor, currency=currency,
                                        destination_id=destination_id, tenant=tenant, event_id=event_id)
            return (status in _TERMINAL), (await self.get(payout_ref) or row)
        if existing.get("status") in _TERMINAL:
            return False, existing                               # already settled — idempotent no-op
        if status not in _TERMINAL:
            return False, existing                               # still pending — nothing to transition
        ver = int(existing.get("version", 0))
        updated = {k: v for k, v in existing.items() if k != "version"}
        updated.update({"status": status, "applied_event_id": event_id, "updated_at": self._now()})
        if amount_minor:
            updated["amount_minor"] = int(amount_minor)
        if currency:
            updated["currency"] = currency.upper()
        if not await self._s.cas(_COLL, payout_ref, ver, updated):
            # Raced with another transition — re-read; first terminal wins (idempotent).
            return False, (await self.get(payout_ref) or updated)
        return True, (await self.get(payout_ref) or updated)

    async def pending_page(self, *, limit: int, after: str | None = None) -> tuple[list[dict], str | None]:
        """A page of PENDING payouts for the reconcile sweep — a scoped, keyset-paged read (never a scan)."""
        return await self._s.find(_COLL, {"status": PAYOUT_PENDING}, limit=max(1, int(limit)),
                                  after=after, sort=("created_at", "asc"))
