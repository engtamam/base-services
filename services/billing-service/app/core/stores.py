"""Repositories over the generic DataStore (tenant-scoped) + sequential invoice numbers."""
from __future__ import annotations

from typing import Any

from app.core.models import (Coupon, Invoice, Payment, Plan, Settlement, Subscription, iso,
                             new_id, now_ts)
from app.ports.datastore import DataStore

_SCAN = 1_000_000


class _Repo:
    collection = ""
    tenant_scoped = True

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def put(self, key: str, value: dict) -> None:
        await self._store.put(self.collection, key, value)

    async def get(self, key: str) -> dict | None:
        return await self._store.get(self.collection, key)

    async def delete(self, key: str) -> None:
        await self._store.delete(self.collection, key)

    async def list_tenant(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(self.collection, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("created", ""), reverse=True)
        return rows[:limit]


async def bump_generation(cache, scope: str) -> None:
    """Invalidate every cached limits lookup for `scope` (a tenant id, or 'global').

    Entitlement limits are cached on the read hot path. The bump lives INSIDE the store
    writes below rather than at the call sites, because there are a dozen call sites and
    forgetting one means an upgrade silently does not take effect until the TTL expires.
    """
    if cache is None:
        return
    try:
        key = f"billinggen:{scope}"
        current = await cache.get(key)
        await cache.set(key, str(int(current or 0) + 1), None)
    except Exception:      # noqa: BLE001 — a cache outage must never fail a write
        pass


class PlanStore(_Repo):
    collection = "plans"                 # global catalog (not tenant-scoped)

    def __init__(self, store: DataStore, cache=None) -> None:
        super().__init__(store)
        self._cache = cache

    async def create(self, p: Plan) -> Plan:
        await self.put(p.id, p.to_dict())
        # Editing the catalogue changes limits for every non-grandfathered tenant.
        await bump_generation(self._cache, "global")
        return p

    async def fetch(self, plan_id: str) -> Plan | None:
        r = await self.get(plan_id)
        return Plan.from_dict(r) if r else None

    async def all(self, limit: int = 100) -> list[dict]:
        return (await self._store.list(self.collection, _SCAN))[:limit]


class CouponStore(_Repo):
    collection = "coupons"

    async def upsert(self, c: Coupon) -> Coupon:
        await self.put(c.code, c.to_dict())
        return c

    async def fetch(self, code: str) -> Coupon | None:
        r = await self.get(code)
        return Coupon.from_dict(r) if r else None


class SubscriptionStore(_Repo):
    collection = "subscriptions"

    def __init__(self, store: DataStore, cache=None) -> None:
        super().__init__(store)
        self._cache = cache

    async def save(self, s: Subscription) -> Subscription:
        await self.put(s.id, s.to_dict())
        # Upgrade / downgrade / cancel must take effect on the NEXT request, not after a TTL.
        await bump_generation(self._cache, s.tenant)
        return s

    async def fetch(self, sub_id: str) -> Subscription | None:
        r = await self.get(sub_id)
        return Subscription.from_dict(r) if r else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return await self.list_tenant(tenant, limit)

    async def active_for(self, tenant: str) -> dict | None:
        """The tenant's active/trialing subscription.

        Still a scan today (the generic DataStore has no secondary index), which is exactly
        why `_active_limits` caches the RESULT by generation instead of calling this per
        request. A real deployment replaces this repo with an indexed query.
        """
        for r in await self._store.list(self.collection, _SCAN):
            if r.get("tenant") == tenant and r.get("status") in ("active", "trialing"):
                return r
        return None

    async def due(self, now: float, limit: int = 100) -> list[Subscription]:
        out = [Subscription.from_dict(r) for r in await self._store.list(self.collection, _SCAN)
               if r.get("status") in ("active", "trialing") and float(r.get("current_period_end", 0)) <= now]
        return out[:limit]


class InvoiceStore(_Repo):
    collection = "invoices"
    _SEQ = "billing_seq"

    async def next_number(self, tenant: str) -> str:
        row = await self._store.get(self._SEQ, tenant)
        n = int(row["n"]) + 1 if row else 1
        await self._store.put(self._SEQ, tenant, {"n": n})
        return f"INV-{n:06d}"

    async def save(self, inv: Invoice) -> Invoice:
        await self.put(inv.id, inv.to_dict())
        return inv

    async def fetch(self, invoice_id: str) -> Invoice | None:
        r = await self.get(invoice_id)
        return Invoice.from_dict(r) if r else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        return await self.list_tenant(tenant, limit)


class PaymentStore(_Repo):
    collection = "payments"

    async def save(self, p: Payment) -> Payment:
        await self.put(p.id, p.to_dict())
        return p

    async def fetch(self, payment_id: str) -> Payment | None:
        r = await self.get(payment_id)
        return Payment.from_dict(r) if r else None

    async def by_provider_id(self, provider_id: str) -> Payment | None:
        for r in await self._store.list(self.collection, _SCAN):
            if r.get("provider_id") == provider_id and provider_id:
                return Payment.from_dict(r)
        return None

    async def due(self, now: float, limit: int = 100) -> list[Payment]:
        out = [Payment.from_dict(r) for r in await self._store.list(self.collection, _SCAN)
               if r.get("status") == "failed" and r.get("next_retry_at") is not None
               and float(r["next_retry_at"]) <= now]
        return out[:limit]


class LedgerStore(_Repo):
    """Append-only money ledger — the reconciliation source of truth.

    Every charge/refund/credit/overage is one immutable row with a running per-tenant
    balance. Invoices/reports derive from this; rows are never mutated or deleted.
    """
    collection = "ledger"

    async def record(self, *, tenant: str, kind: str, amount_minor: int, currency: str,
                     invoice_id: str = "", source: str = "", period: str = "") -> dict:
        rows = [r for r in await self._store.list(self.collection, _SCAN) if r.get("tenant") == tenant]
        balance = sum(int(r.get("amount_minor", 0)) for r in rows) + int(amount_minor)
        entry = {"id": new_id("led_"), "tenant": tenant, "kind": kind, "amount_minor": int(amount_minor),
                 "currency": currency, "invoice_id": invoice_id, "source": source, "period": period,
                 "balance_after": balance, "created": iso()}
        await self.put(entry["id"], entry)
        return entry

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(self.collection, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("created", ""))
        return rows[-limit:]


class IdempotencyStore(_Repo):
    """Deterministic idempotency — a double-click / retry can't double-charge."""
    collection = "idempo"

    async def seen(self, key: str) -> dict | None:
        return await self.get(key)

    async def mark(self, key: str, result: dict) -> None:
        await self.put(key, {"key": key, "result": result, "created": iso()})


class UsageStore(_Repo):
    collection = "usage"

    async def record(self, tenant: str, subscription_id: str, metric: str, quantity: int) -> dict:
        rec = {"id": new_id("use_"), "tenant": tenant, "subscription_id": subscription_id,
               "metric": metric, "quantity": int(quantity), "ts": now_ts()}
        await self.put(rec["id"], rec)
        return rec

    async def aggregate(self, subscription_id: str, metric: str,
                        start: float, end: float) -> int:
        total = 0
        for r in await self._store.list(self.collection, _SCAN):
            if (r.get("subscription_id") == subscription_id and r.get("metric") == metric
                    and start <= float(r.get("ts", 0)) < end):
                total += int(r.get("quantity", 0))
        return total


class SettlementStore(_Repo):
    """Off-gateway settlements + the (method, issuer, reference) uniqueness claim."""
    collection = "settlements"

    # The claim lives in its own collection so it can be taken ATOMICALLY, before anything
    # else is written. `incr_if_below(..., delta=1, limit=1)` is a 1-of-1 counter: the first
    # caller gets (True, 1), every later one gets (False, 1). No new port primitive needed —
    # the same operation that stops the 26th seat stops the second settlement of one transfer.
    _CLAIMS = "settlement_claims"

    # Maps a payment identity -> the settlement that recorded it. A RETRY of the same triple
    # against the same invoice is idempotent; the same triple against a DIFFERENT invoice is a
    # second transfer that never happened, and is refused.
    _INDEX = "settlement_index"

    async def claim_identity(self, key: str) -> bool:
        """True if THIS caller now owns the payment identity. False if it was already taken."""
        applied, _ = await self._store.incr_if_below(self._CLAIMS, key, 1, 1)
        return applied

    async def index_identity(self, key: str, settlement_id: str, invoice_id: str) -> None:
        await self._store.put(self._INDEX, key, {"settlement_id": settlement_id,
                                                 "invoice_id": invoice_id})

    async def resolve_identity(self, key: str) -> dict | None:
        return await self._store.get(self._INDEX, key)

    async def release_identity(self, key: str) -> None:
        """Give the claim back when the settlement it guarded could not be written."""
        await self._store.incr_if_below(self._CLAIMS, key, -1, None)

    async def save(self, s: Settlement) -> Settlement:
        await self.put(s.id, s.to_dict())
        return s

    async def fetch(self, settlement_id: str) -> Settlement | None:
        r = await self.get(settlement_id)
        return Settlement.from_dict(r) if r else None

    async def for_invoice(self, invoice_id: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(self.collection, _SCAN)
                if r.get("invoice_id") == invoice_id]
        rows.sort(key=lambda r: r.get("created", ""))
        return rows[:limit]
