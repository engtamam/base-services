"""The durable path from "they paid" to "they got what they paid for".

A customer buys a plan that includes credits, or buys a credit pack outright. Something has to put
those credits in their wallet, and that something may not be an event: the bus is at-most-once, so
a subscriber restarting at the wrong moment means the credits never arrive at all.

SO EVERY GRANT IS WRITTEN DOWN BEFORE IT IS DELIVERED. The row is the promise. Delivery is a retry
against that row, and a grant that has not been delivered stays visible for ever rather than being
forgotten — an undelivered grant should be an operator's problem, never a customer's.

THE THREE FAILURES THIS IS SHAPED AROUND

1. *Granted twice.* The outbox retries and a reconciliation sweep re-reads the same invoices, so
   the same grant WILL be offered more than once. `source` is derived from the invoice, never from
   a clock or a random id, and the receiving wallet de-duplicates on it. Money given away is not
   recoverable by an apology.

2. *Granted never.* If the process dies between the payment and the grant row, nothing in the
   outbox knows a grant was owed. `reconcile()` exists for exactly that: it reads PAID INVOICES —
   the durable fact — and creates any grant row that is missing. A read is self-healing; a missed
   in-memory step is wrong for ever.

3. *Retried for ever.* A wallet that refuses because the fund does not exist will refuse the
   thousandth time too. Permanent refusals are marked failed and stop consuming attempts, because a
   queue that only grows hides the misconfiguration that caused it.

DISABLED BY DEFAULT. An adopter who does not run wallet-service must not acquire a background task
that fails for ever against a service they never deployed.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.ports.wallet import WalletRefused, WalletUnavailable

_log = logging.getLogger(__name__)

_COLL = "credit_grants"

PENDING = "pending"
DELIVERED = "delivered"
FAILED = "failed"


def source_for(invoice_id: str) -> str:
    """The idempotency key the wallet de-duplicates on.

    Derived from the INVOICE, so the same grant offered by a retry, by a reconciliation sweep, or
    by a redelivered event is one grant. Anything containing a clock or a random id would make each
    attempt look like a new credit, which is how a customer ends up with three times what they paid
    for.
    """
    return f"billing:invoice:{invoice_id}"


def clawback_source_for(invoice_id: str) -> str:
    """The idempotency key for REVERSING a grant — deliberately DISTINCT from `source_for`.

    This is the single most important correctness point of the clawback. The wallet de-duplicates
    on `source`; a clawback that reused the grant's key would be swallowed as if it were the
    original credit and reverse nothing. A distinct `:clawback` suffix makes the reversal its own
    idempotent promise: redelivered once, never confused with the credit it undoes.
    """
    return f"billing:invoice:{invoice_id}:clawback"


@dataclass
class Grant:
    id: str
    tenant: str
    source: str
    fund: str
    amount_minor: int
    reason: str = ""
    status: str = PENDING
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""
    created: str = ""
    delivered_at: str = ""
    # When the granted credits lapse. Set for a plan whose allowance does not roll over; None when
    # it does. Stored on the row so a retry days later still delivers the ORIGINAL expiry rather
    # than one computed from whenever the retry happened to succeed.
    expires_at: float | None = None
    # "grant" delivers via wallet.credit; "clawback" reverses it via wallet.debit. One row shape,
    # one outbox, one reconcile — the reversal is the same durable promise as the grant it undoes.
    kind: str = "grant"
    # Only meaningful for a clawback: let the balance go below zero when the refunded credits are
    # already spent, so a customer cannot keep both the money and the goods.
    allow_negative: bool = False
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> "Grant":
        known = {k: v for k, v in row.items() if k in cls.__dataclass_fields__}
        return cls(**known)


class GrantOutbox:
    def __init__(self, store, wallet, *, clock, iso, cfg: dict[str, Any], emitter=None) -> None:
        self._s = store
        self._wallet = wallet          # None when no wallet is wired: recording still happens
        self._clock = clock
        self._iso = iso
        self._cfg = cfg
        # Optional, best-effort: emits grant.clawback_failed when a reversal is permanently refused
        # (the credits were spent and allow_negative was off). A dropped hint never blocks the
        # durable row, which stays FAILED and visible to an operator regardless.
        self._emitter = emitter

    # ── record ────────────────────────────────────────────────────────────────
    async def record(self, *, tenant: str, invoice_id: str, fund: str, amount_minor: int,
                     reason: str = "", expires_at: float | None = None,
                     meta: dict | None = None) -> Grant | None:
        """Write the promise down. Returns None when there is nothing to grant.

        The id IS the source, so recording the same invoice twice overwrites one row instead of
        queueing a second delivery. Two rows would both be delivered, and the wallet would
        de-duplicate them — but only because it happens to key on the same string. Relying on the
        far side to clean up after us is not a design, it is a coincidence.
        """
        if amount_minor <= 0:
            # Nothing to grant. A zero grant would be an outbox row that delivers nothing for ever,
            # and a negative one is a refund pretending to be a credit.
            return None
        source = source_for(invoice_id)
        existing = await self._s.get(_COLL, source)
        if existing:
            return Grant.from_dict(existing)
        grant = Grant(id=source, tenant=tenant, source=source, fund=fund,
                      amount_minor=int(amount_minor), reason=reason,
                      expires_at=expires_at, created=self._iso(), meta=meta or {})
        await self._s.put(_COLL, grant.id, grant.to_dict())
        return grant

    async def record_clawback(self, *, tenant: str, invoice_id: str, fund: str, amount_minor: int,
                              reason: str = "", allow_negative: bool = False) -> Grant | None:
        """Write down the promise to REVERSE a grant. Returns None when there is nothing to reverse.

        Keyed on `clawback_source_for` — DISTINCT from the grant — so a redelivered refund reverses
        once and is never mistaken for the credit. Recording twice overwrites one row, exactly like
        a grant, so a double refund produces one clawback.
        """
        if amount_minor <= 0:
            return None
        source = clawback_source_for(invoice_id)
        existing = await self._s.get(_COLL, source)
        if existing:
            return Grant.from_dict(existing)
        row = Grant(id=source, tenant=tenant, source=source, fund=fund,
                    amount_minor=int(amount_minor), reason=reason, kind="clawback",
                    allow_negative=allow_negative, created=self._iso())
        await self._s.put(_COLL, row.id, row.to_dict())
        return row

    # ── deliver ───────────────────────────────────────────────────────────────
    async def deliver(self, grant: Grant) -> bool:
        """One delivery attempt. Never raises: a failure is recorded and retried.

        A grant delivers via wallet.credit; a clawback reverses via wallet.debit. Same row, same
        retry and refusal handling — a reversal is the durable mirror of the grant, not a special
        case bolted on beside it.
        """
        if self._wallet is None:
            return False
        try:
            if grant.kind == "clawback":
                await self._wallet.debit(tenant=grant.tenant, fund=grant.fund,
                                         amount_minor=grant.amount_minor, source=grant.source,
                                         reason=grant.reason, allow_negative=grant.allow_negative)
            else:
                await self._wallet.credit(tenant=grant.tenant, fund=grant.fund,
                                          amount_minor=grant.amount_minor,
                                          source=grant.source, reason=grant.reason,
                                          expires_at=grant.expires_at)
        except WalletRefused as exc:
            # Permanent. Retrying a thousand times cannot make a fund exist, and a queue that only
            # grows hides the misconfiguration that caused it. For a clawback the common permanent
            # refusal is "insufficient balance, allow_negative off" — the credits were spent — which
            # an operator must see, so we mark it failed AND emit grant.clawback_failed.
            grant.status = FAILED
            grant.last_error = f"refused: {type(exc).__name__}"
            await self._s.put(_COLL, grant.id, grant.to_dict())
            if grant.kind == "clawback":
                _log.error("grant_clawback_failed",
                           extra={"grant": grant.id, "tenant": grant.tenant})
                await self._emit("grant.clawback_failed", {
                    "tenant": grant.tenant, "fund": grant.fund,
                    "amount_minor": grant.amount_minor, "source": grant.source,
                    "reason": grant.last_error})
            else:
                _log.error("grant_refused_permanently",
                           extra={"grant": grant.id, "tenant": grant.tenant})
            return False
        except (WalletUnavailable, Exception) as exc:  # noqa: BLE001 — any transport failure
            grant.attempts += 1
            base = self._cfg["grant_retry_base_seconds"]
            cap = self._cfg["grant_retry_max_seconds"]
            grant.next_attempt_at = self._clock() + min(base * (2 ** grant.attempts), cap)
            grant.last_error = type(exc).__name__
            # Stays PENDING however many times it fails. There is no attempt cap here on purpose:
            # giving up would silently keep money the customer paid for, and an operator can see a
            # grant that is old and undelivered far more easily than one that was dropped.
            await self._s.put(_COLL, grant.id, grant.to_dict())
            return False

        grant.status = DELIVERED
        grant.delivered_at = self._iso()
        grant.last_error = ""
        await self._s.put(_COLL, grant.id, grant.to_dict())
        return True

    async def _emit(self, event: str, payload: dict) -> None:
        """Best-effort hint. A dropped emit never touches the durable row."""
        if self._emitter is None:
            return
        try:
            await self._emitter.emit(event, payload)
        except Exception:  # noqa: BLE001 — the row is the truth; the event is a courtesy
            _log.warning("grant_event_emit_failed", extra={"event": event})

    async def drain(self, *, limit: int = 100) -> dict[str, int]:
        """Deliver every grant that is due. Driven by the platform's scheduler."""
        now = self._clock()
        rows, _ = await self._s.find(_COLL, {"status": PENDING}, limit=limit,
                                     sort=("created", "asc"))
        delivered = deferred = 0
        for row in rows:
            grant = Grant.from_dict(row)
            if grant.next_attempt_at and grant.next_attempt_at > now:
                deferred += 1
                continue
            if await self.deliver(grant):
                delivered += 1
        return {"considered": len(rows), "delivered": delivered, "deferred": deferred}

    # ── self-heal ─────────────────────────────────────────────────────────────
    async def reconcile(self, *, invoices, limit: int = 200) -> dict[str, int]:
        """Create grant rows for paid invoices that never got one.

        This is what makes the whole path survivable. If the process died between recording the
        payment and recording the grant, the outbox has no idea anything was owed — but the PAID
        INVOICE is still there, and it is the durable fact. Rebuilding from it means a crash costs
        a delay rather than a customer's credits.
        """
        rows, _ = await invoices.find_paid(limit=limit)
        created = 0
        for inv in rows:
            amount = self._grantable(inv)
            if amount <= 0:
                continue
            if await self._s.get(_COLL, source_for(inv["id"])):
                continue
            await self.record(tenant=inv["tenant"], invoice_id=inv["id"],
                              fund=self._cfg["grant_fund"], amount_minor=amount,
                              reason="reconciled")
            created += 1
        return {"scanned": len(rows), "created": created}

    async def reconcile_clawbacks(self, *, invoices, limit: int = 200) -> dict[str, int]:
        """Create clawback rows for voided invoices whose delivered grant was never reversed.

        The mirror of `reconcile`: if the process died between voiding an invoice and recording the
        clawback, nothing in the outbox knows a reversal is owed — but the VOIDED INVOICE is the
        durable fact. A clawback is created only when the original grant was actually DELIVERED (an
        undelivered grant is cancelled, not reversed — there is nothing in the wallet to take back)
        and no clawback row exists yet.
        """
        rows, _ = await invoices.find_voided(limit=limit)
        created = 0
        for inv in rows:
            amount = self._grantable(inv)
            if amount <= 0:
                continue
            grant = await self._s.get(_COLL, source_for(inv["id"]))
            if not grant or grant.get("status") != DELIVERED:
                continue                      # nothing delivered → nothing to reverse
            if await self._s.get(_COLL, clawback_source_for(inv["id"])):
                continue                      # already reversing
            await self.record_clawback(
                tenant=inv["tenant"], invoice_id=inv["id"], fund=self._cfg["grant_fund"],
                amount_minor=amount, reason="reconciled",
                allow_negative=bool(self._cfg.get("clawback_allow_negative", True)))
            created += 1
        return {"scanned": len(rows), "created": created}

    def _grantable(self, inv: dict) -> int:
        """How much a paid invoice is worth in wallet funds.

        Read from the invoice's own recorded grant amount rather than recomputed from the live
        plan: a plan repriced after the sale must not change what an old invoice grants.
        """
        return int((inv.get("meta") or {}).get("grant_minor", 0) or 0)
