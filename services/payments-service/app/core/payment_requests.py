"""PaymentRequest — the conduit's one money primitive.

A PaymentRequest is the platform's server-authoritative record of "shop T asked customer C to pay
AMOUNT for order R via its own gateway". The AMOUNT and CURRENCY are fixed HERE, at creation, and are
never taken from a webhook — a gateway callback can only move an existing request to a terminal
state, and only if the amount it reports matches what we recorded. That is what stops a forged or
replayed "paid" for the wrong sum.

Lifecycle: pending → completed | failed | cancelled | expired. The terminal transition is a
version-pinned compare-and-set, so a webhook and a reconcile racing on the same request produce
exactly ONE completion (and one `payment.completed` event). The row is CREATED via cas insert-if-
absent so it always carries a version (a row written by a plain put() cannot be cas-updated on the
postgres adapter — see the identity refresh-rotation incident).
"""
from __future__ import annotations

import time
import uuid

from app.ports.payment_gateway import CANCELLED, FAILED, PENDING, SUCCEEDED

_COLL = "payment_requests"
# The atomic idempotency guard for create_link. A claim is inserted (cas insert-if-absent) on the
# LOGICAL order key (tenant, reference) BEFORE the gateway bill is minted, so exactly one concurrent
# create wins the right to mint — the others resolve to the winner's request. It maps the logical key
# to the unguessable request id; the request id (the public redirector token) stays a random uuid, so
# the claim being derivable from (tenant, reference) never makes a pay-link guessable.
_CLAIM_COLL = "payment_claims"

# Public lifecycle states (SUCCEEDED/FAILED/CANCELLED/PENDING are the gateway's normalised statuses).
COMPLETED = "completed"
EXPIRED = "expired"
_TERMINAL = (COMPLETED, FAILED, CANCELLED, EXPIRED)


def _now() -> float:
    return time.time()


class PaymentRequestError(ValueError):
    """A request that cannot be created or transitioned safely."""


class PaymentRequestStore:
    def __init__(self, store, *, now=_now) -> None:
        self._s = store
        self._now = now

    async def create(self, *, tenant: str, reference: str, amount_minor: int, currency: str,
                     provider: str, provider_ref: str, redirect_url: str,
                     ttl_seconds: int, expires_at_provider: str = "",
                     request_id: str | None = None) -> dict:
        """Insert a new pending request. `request_id` is the unguessable token (also the redirector
        key) — supplied by the caller that won the (tenant, reference) claim, or freshly minted here.
        Created via cas insert-if-absent so the row carries version=1 and can be transitioned later."""
        if amount_minor <= 0:
            raise PaymentRequestError("amount_minor must be positive")
        rid = request_id or uuid.uuid4().hex
        now = self._now()
        row = {
            "id": rid, "tenant": tenant, "reference": reference,
            "amount_minor": int(amount_minor), "currency": currency,
            "status": PENDING, "provider": provider, "provider_ref": provider_ref,
            "redirect_url": redirect_url,
            "created_at": now, "expires_at": now + int(ttl_seconds),
            "expires_at_provider": expires_at_provider,
            "applied_event_id": "", "paid_amount_minor": 0, "paid_currency": "",
            "amount_mismatch": False,
        }
        if not await self._s.cas(_COLL, rid, 0, row):        # insert-if-absent
            raise PaymentRequestError("payment request id collision")
        return await self.get(rid)

    async def get(self, request_id: str) -> dict | None:
        return await self._s.get(_COLL, request_id)

    # ── atomic (tenant, reference) claim — the double-charge guard ─────────────────
    @staticmethod
    def _claim_key(tenant: str, reference: str) -> str:
        # Length-prefix the tenant so ("a","b|c") and ("a|b","c") can never collide (the
        # tenant-scoped-idempotency rule). The value is the logical order key, not a secret.
        return f"{len(tenant)}:{tenant}|{reference}"

    async def claim(self, *, tenant: str, reference: str, request_id: str) -> tuple[bool, dict | None]:
        """Try to claim (tenant, reference) for `request_id`. Returns (won, current_claim_row). The
        winner (won=True) is the ONLY caller allowed to mint a gateway bill for this order; a loser
        reads the returned claim to find whoever won."""
        key = self._claim_key(tenant, reference)
        row = {"id": key, "tenant": tenant, "reference": reference,
               "request_id": request_id, "created_at": self._now()}
        won = await self._s.cas(_CLAIM_COLL, key, 0, row)
        return won, await self._s.get(_CLAIM_COLL, key)

    async def get_claim(self, *, tenant: str, reference: str) -> dict | None:
        return await self._s.get(_CLAIM_COLL, self._claim_key(tenant, reference))

    async def steal_claim(self, claim_row: dict, *, request_id: str) -> bool:
        """Take over a STALE claim (its request expired/failed) for a fresh request_id, version-pinned
        so only one caller can steal it. Returns True for the single winner."""
        ver = int(claim_row.get("version", 0))
        updated = {k: v for k, v in claim_row.items() if k != "version"}
        updated.update({"request_id": request_id, "created_at": self._now()})
        return await self._s.cas(_CLAIM_COLL, claim_row["id"], ver, updated)

    async def release_claim(self, *, tenant: str, reference: str) -> bool:
        """Drop a claim so the order can be re-attempted — called when the gateway mint FAILS, so a
        failed attempt never permanently blocks a retry for that order."""
        return await self._s.delete(_CLAIM_COLL, self._claim_key(tenant, reference))

    async def by_provider_ref(self, *, tenant: str, provider_ref: str) -> dict | None:
        """Resolve the request a gateway callback refers to. Keyed by (tenant, provider_ref) so one
        gateway's id can never resolve another tenant's request (cross-tenant guard)."""
        if not provider_ref:
            return None
        rows, _ = await self._s.find(
            _COLL, {"tenant": tenant, "provider_ref": provider_ref}, limit=2, sort=("created_at", "desc"))
        return rows[0] if rows else None

    async def active_by_reference(self, *, tenant: str, reference: str) -> dict | None:
        """An existing non-expired request for the same (tenant, order reference) — the idempotency
        key that stops a second create from minting a second link (and a possible double charge)."""
        if not reference:
            return None
        rows, _ = await self._s.find(
            _COLL, {"tenant": tenant, "reference": reference}, limit=10, sort=("created_at", "desc"))
        now = self._now()
        for r in rows:
            if r.get("status") == COMPLETED:
                return r
            if r.get("status") == PENDING and float(r.get("expires_at", 0)) > now:
                return r
        return None

    def is_expired(self, row: dict) -> bool:
        return row.get("status") == PENDING and float(row.get("expires_at", 0)) <= self._now()

    async def _transition(self, row: dict, changes: dict) -> tuple[bool, dict]:
        """Version-pinned CAS from the row we read. Returns (won, current_row). A lost CAS means a
        concurrent writer already transitioned it — the caller re-reads and treats it as done."""
        ver = int(row.get("version", 0))
        updated = {k: v for k, v in row.items() if k != "version"}
        updated.update(changes)
        won = await self._s.cas(_COLL, row["id"], ver, updated)
        return won, (await self.get(row["id"]) or updated)

    async def mark_expired(self, row: dict) -> tuple[bool, dict]:
        if row.get("status") != PENDING:
            return False, row
        return await self._transition(row, {"status": EXPIRED})

    async def apply_outcome(self, row: dict, *, status: str, event_id: str,
                            paid_amount_minor: int, paid_currency: str,
                            amount_mismatch: bool) -> tuple[bool, dict]:
        """Move a PENDING request to a terminal state exactly once. Returns (transitioned_now, row).
        A row already terminal returns (False, row) — the idempotent no-op a replayed webhook hits."""
        if row.get("status") in _TERMINAL:
            return False, row
        final = COMPLETED if status == SUCCEEDED else status if status in (FAILED, CANCELLED) else PENDING
        if final == PENDING:                                  # not actionable yet
            return False, row
        return await self._transition(row, {
            "status": final, "applied_event_id": event_id,
            "paid_amount_minor": int(paid_amount_minor), "paid_currency": paid_currency,
            "amount_mismatch": bool(amount_mismatch)})
