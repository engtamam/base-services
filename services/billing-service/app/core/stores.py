"""Repositories over the generic DataStore (tenant-scoped) + sequential invoice numbers."""
from __future__ import annotations

import asyncio
import random
from typing import Any

from app.core.models import (Coupon, Invoice, Payment, Plan, Settlement, Subscription, iso,
                             new_id, now_ts)
from app.ports.datastore import DataStore

# Bounds for the few reads that legitimately cross every tenant: the plan CATALOGUE (tens of
# rows, not one per tenant) and the billing SWEEPERS, which are cron jobs whose whole job is to
# visit every row. Each such call is marked `# sweeper:` and says why. Nothing on a request path
# may call scan_all() — a guard test enforces it. These are three orders of magnitude below the
# old blanket 1_000_000, so a truncation is a paging bug the operator sees, not a silent
# arbitrary subset a customer sees.
_CATALOG_MAX = 1_000
_SWEEP_PAGE = 500


class LedgerContention(Exception):
    """Too much contention on one tenant's balance. NOTHING was written; the caller may retry."""


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

    async def list_tenant(self, tenant: str, limit: int = 100,
                          after: str | None = None) -> list[dict]:
        """One tenant's rows, newest first. The filter is pushed into the datastore.

        This read used to cost the size of EVERY tenant's data, and past the scan cap it
        returned an arbitrary subset of the caller's own rows.
        """
        rows, _ = await self._store.find(self.collection, {"tenant": tenant},
                                         limit=limit, after=after, sort=("created", "desc"))
        return rows

    # Filters a caller may narrow a listing by. A WHITELIST per repo, because the alternative —
    # passing query parameters into the datastore — lets anyone filter by any stored field and
    # turns a list endpoint into an arbitrary query engine over other people's rows.
    filterable: tuple[str, ...] = ()

    # The platform view MAY narrow by tenant — it is the cross-tenant read, so scoping it to one
    # customer is a legitimate filter, not a wall breach. The tenant-scoped `browse` still forbids a
    # tenant filter key, because there the tenant is the argument and must never be overridable.
    platform_filterable: tuple[str, ...] = ()

    async def browse_platform(self, *, where: dict, limit: int = 100, after: str | None = None,
                              sort: tuple[str, str] = ("created", "desc")) -> tuple[list[dict], str | None]:
        """A page of rows ACROSS ALL TENANTS, narrowed by a mandatory filter.

        This deliberately crosses the tenancy wall, so it is fenced on every side. It requires a
        non-empty `where` — there is no "every row ever" call, both because find() forbids an empty
        filter and because an unfiltered platform dump is a data-exfiltration primitive, not a
        feature. The filter keys are still whitelisted, so a caller cannot probe by arbitrary
        content. And the route above it is gated on platform.billing alone and audits every call.
        """
        allowed = set(self.filterable) | set(self.platform_filterable)
        query = {}
        for key, value in where.items():
            if key not in allowed:
                raise ValueError(f"NOT_FILTERABLE:{key}")
            if value not in (None, ""):
                query[key] = value
        if not query:
            raise ValueError("PLATFORM_LISTING_NEEDS_A_FILTER")
        return await self._store.find(self.collection, query, limit=limit, after=after, sort=sort)

    async def browse(self, tenant: str, *, limit: int = 100, after: str | None = None,
                     where: dict | None = None,
                     sort: tuple[str, str] = ("created", "desc")) -> tuple[list[dict], str | None]:
        """A page of one tenant's rows, narrowed and RESUMABLE.

        `list_tenant` discarded the cursor the datastore already returned, so a listing was
        capped rather than paginated: past the limit the remaining rows were simply invisible,
        with nothing in the response to say so. A UI cannot show a second page of invoices it is
        never told exists.
        """
        query = {"tenant": tenant}
        for key, value in (where or {}).items():
            if key not in self.filterable:
                raise ValueError(f"NOT_FILTERABLE:{key}")
            if value not in (None, ""):
                query[key] = value
        return await self._store.find(self.collection, query, limit=limit, after=after, sort=sort)


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
        # sweeper: the plan CATALOGUE is global and small (one row per plan, not per tenant).
        # There is no tenant to scope by; reading it whole is the operation, not a shortcut.
        return (await self._store.scan_all(self.collection, _CATALOG_MAX))[:limit]


class CouponStore(_Repo):
    collection = "coupons"
    # Global, like the plan catalogue: a coupon code is not owned by a tenant.
    tenant_scoped = False

    async def upsert(self, c: Coupon) -> Coupon:
        await self.put(c.code, c.to_dict())
        return c

    async def fetch(self, code: str) -> Coupon | None:
        r = await self.get(code)
        return Coupon.from_dict(r) if r else None

    async def patch(self, code: str, changes: dict) -> Coupon | None:
        """Change some fields of an existing coupon, leaving the rest alone.

        `redeemed` is NEVER writable through here. A full-object write was the only way to edit a
        coupon, and it reset the redemption counter to zero — so correcting a typo in a campaign
        silently resurrected a coupon that customers had already used up. The counter belongs to
        what happened, not to what an operator is editing.
        """
        row = await self.get(code)
        if row is None:
            return None
        for key, value in changes.items():
            if key in ("code", "redeemed"):
                continue
            if value is not None:
                row[key] = value
        await self.put(code, row)
        return Coupon.from_dict(row)

    async def redeem(self, code: str) -> int:
        """Count one use. Returns the new total.

        Nothing called anything like this before: `redeemed` was declared on the model and never
        written, so `max_redemptions` was a number an operator set and the service ignored.
        """
        row = await self.get(code)
        if row is None:
            return 0
        row["redeemed"] = int(row.get("redeemed", 0) or 0) + 1
        await self.put(code, row)
        return row["redeemed"]

    async def all(self, limit: int = 100) -> list[dict]:
        """The whole coupon catalogue."""
        # sweeper: the coupon CATALOGUE is global and small (one row per campaign, not per
        # tenant), exactly like the plan catalogue above. There is no tenant to scope by, so
        # reading it whole IS the operation rather than a shortcut around an index — and it is
        # bounded by _CATALOG_MAX so a runaway campaign list cannot become an unbounded read.
        return (await self._store.scan_all(self.collection, _CATALOG_MAX))[:limit]

    async def remove(self, code: str) -> bool:
        """Delete a coupon outright.

        Offered alongside deactivation because they are different intentions: a mistyped code that
        was never used should disappear, while a campaign that customers redeemed should be turned
        OFF and kept, so the history of what was honoured survives.
        """
        if await self.get(code) is None:
            return False
        await self.delete(code)
        return True


class SubscriptionStore(_Repo):
    filterable = ("status", "plan_id")
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

    async def billable_for(self, tenant: str, statuses: tuple[str, ...],
                           limit: int = 200) -> list[dict]:
        """EVERY subscription a tenant is currently billable on. One indexed read.

        active_for() returns ONE row, which was correct while a tenant had one subscription and
        silently wrong the moment they bought two: entitlements resolved from whichever row sorted
        first, so a customer who paid for three things could use one of them.

        Still a single query. This sits on the entitlement hot path — feature-flag-service calls
        the resolver once per flag evaluation — so N subscriptions must not become N reads. The
        status set is pushed into the datastore rather than filtered in Python, for the same reason
        active_for() was fixed: past the cap, a paying tenant could be told it had nothing.

        Ordered OLDEST FIRST and by id, so the order is total and stable. REPLACE takes the last
        contribution, which only means "newest wins" if the order is defined — and two rows sharing
        a `created` value would otherwise straddle a page boundary differently on each read, making
        the same tenant resolve two different ways.
        """
        rows, _ = await self._store.find(self.collection,
                                         {"tenant": tenant, "status": list(statuses)},
                                         limit=limit, sort=("created", "asc"))
        return sorted(rows, key=lambda r: (r.get("created", ""), r.get("id", "")))

    async def active_for(self, tenant: str) -> dict | None:
        """The tenant's active/trialing subscription. Indexed lookup, one row.

        This is on the entitlement hot path. It used to read every subscription of every tenant
        and filter in Python — and past the cap, a tenant with an active subscription could be
        told it had none, which fails a paying customer's request closed. Both the tenant and
        the status are pushed into the datastore now; the cache in front of it is an
        optimisation, no longer the only thing keeping the read affordable.
        """
        rows, _ = await self._store.find(self.collection,
                                         {"tenant": tenant, "status": ["active", "trialing"]},
                                         limit=1, sort=("created", "desc"))
        return rows[0] if rows else None

    async def due(self, now: float, limit: int = 100) -> list[Subscription]:
        """Subscriptions whose period has ended. The renewal sweeper's queue.

        `status` is pushed down; `current_period_end` is a numeric range the equality-only
        filter cannot express, so it is compared here — over the ACTIVE subscriptions only,
        paged, never over cancelled history.
        """
        out: list[Subscription] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(
                self.collection, {"status": ["active", "trialing"]},
                limit=_SWEEP_PAGE, after=cursor, sort=("created", "asc"))
            out.extend(Subscription.from_dict(r) for r in rows
                       if float(r.get("current_period_end", 0)) <= now)
            if not cursor:
                break
        return out[:limit]


class InvoiceStore(_Repo):
    collection = "invoices"
    # Status is what a finance UI filters on: unpaid, paid, written off. Nothing else is
    # exposed — an amount or a buyer name would let a caller probe rows by content.
    filterable = ("status", "subscription_id")
    # The platform (all-tenants) view may additionally narrow to one tenant.
    platform_filterable = ("tenant",)
    _SEQ = "billing_seq"

    async def next_number(self, tenant: str) -> str:
        row = await self._store.get(self._SEQ, tenant)
        n = int(row["n"]) + 1 if row else 1
        await self._store.put(self._SEQ, tenant, {"n": n})
        return f"INV-{n:06d}"

    async def save(self, inv: Invoice) -> Invoice:
        await self.put(inv.id, inv.to_dict())
        return inv

    async def find_paid(self, *, limit: int = 200) -> tuple[list[dict], str]:
        """Paid invoices, for the grant reconciliation sweep.

        A FILTERED find, never a scan: this runs on a schedule against every tenant's invoice
        history, and a full-collection read there grows without bound as the platform succeeds.
        """
        return await self._store.find(self.collection, {"status": "paid"}, limit=limit,
                                      sort=("paid_at", "asc"))

    async def find_voided(self, *, limit: int = 200) -> tuple[list[dict], str]:
        """Voided (refunded / charged-back) invoices, for the grant-clawback reconciliation sweep.

        The mirror of `find_paid`: a FILTERED find, never a scan, so healing a dropped clawback
        costs one bounded read per sweep rather than a full-history scan that grows for ever.
        """
        return await self._store.find(self.collection, {"status": "void"}, limit=limit,
                                      sort=("paid_at", "asc"))

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
        """Resolve a gateway webhook to its payment. On the webhook hot path.

        An empty `provider_id` must match NOTHING. It used to be guarded by an `and provider_id`
        inside the loop; here the guard comes first, because `find({"provider_id": ""})` would
        happily return every payment that never reached a gateway.
        """
        if not provider_id:
            return None
        rows, _ = await self._store.find(self.collection, {"provider_id": provider_id}, limit=1)
        return Payment.from_dict(rows[0]) if rows else None

    async def due(self, now: float, limit: int = 100) -> list[Payment]:
        """Failed payments ready for another attempt. The dunning sweeper's queue.

        `status` is pushed down; `next_retry_at` is a time comparison, done here over the failed
        payments only — not over every successful payment ever taken.
        """
        out: list[Payment] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(self.collection, {"status": "failed"},
                                                  limit=_SWEEP_PAGE, after=cursor,
                                                  sort=("created", "asc"))
            out.extend(Payment.from_dict(r) for r in rows
                       if r.get("next_retry_at") is not None and float(r["next_retry_at"]) <= now)
            if not cursor:
                break
        return out[:limit]


class LedgerStore(_Repo):
    """Append-only money ledger — the reconciliation source of truth.

    Every charge/refund/credit/overage is one immutable row with a running per-tenant
    balance. Invoices/reports derive from this; rows are never mutated or deleted.
    """
    collection = "ledger"

    _BALANCES = "ledger_balances"      # tenant -> {balance_minor, version}

    def __init__(self, store: DataStore, *, cas_max_retries: int = 20,
                 cas_backoff_ms: int = 12) -> None:
        super().__init__(store)
        self._retries = int(cas_max_retries)
        self._backoff_ms = int(cas_backoff_ms)

    async def balance(self, tenant: str) -> int:
        row = await self._store.get(self._BALANCES, tenant)
        return int((row or {}).get("balance_minor", 0))

    async def record(self, *, tenant: str, kind: str, amount_minor: int, currency: str,
                     invoice_id: str = "", source: str = "", period: str = "") -> dict:
        """Append a row and advance the MATERIALIZED balance atomically.

        This used to read every ledger row for the tenant and sum them — on every write. Past
        the scan cap the sum was taken over an arbitrary subset, so `balance_after` was not a
        slow balance. It was a WRONG one, written into an immutable row and sold as the
        reconciliation source of truth.

        The balance is now a projection maintained under compare-and-swap, exactly as
        wallet-service does, and `reconcile()` proves it equals the sum of the rows.

        Losers back off with FULL JITTER before retrying. Without it, every loser re-reads in
        the same instant and collides again: measured against real PostgreSQL, 50 concurrent
        charges on one tenant exhausted all 20 retries and raised. Nothing was written — a
        losing CAS changes nothing by construction — but it is a failure the caller should
        never have seen. The in-memory adapter cannot show this: its `asyncio.Lock` serialises
        the writers instead of letting them race.
        """
        delta = int(amount_minor)
        for attempt in range(self._retries):
            if attempt and self._backoff_ms:
                cap = min(self._backoff_ms * (2 ** (attempt - 1)), self._backoff_ms * 32)
                await asyncio.sleep(random.uniform(0, cap) / 1000.0)  # noqa: S311 - jitter, not crypto
            row = await self._store.get(self._BALANCES, tenant)
            version = int((row or {}).get("version", 0))
            balance = int((row or {}).get("balance_minor", 0)) + delta
            if await self._store.cas(self._BALANCES, tenant, version,
                                     {"tenant": tenant, "balance_minor": balance}):
                break
        else:
            raise LedgerContention("LEDGER_CONTENTION")

        entry = {"id": new_id("led_"), "tenant": tenant, "kind": kind, "amount_minor": delta,
                 "currency": currency, "invoice_id": invoice_id, "source": source, "period": period,
                 "balance_after": balance, "created": iso()}
        await self.put(entry["id"], entry)
        return entry

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        """Newest first, paged. The tenant filter is pushed into the datastore."""
        rows, _ = await self._store.find(self.collection, {"tenant": tenant},
                                         limit=limit, after=after, sort=("created", "desc"))
        return list(reversed(rows))          # callers expect oldest-first within the page

    async def reconcile(self, tenant: str) -> dict:
        """Prove the projection equals the truth. Pages the ledger; never a capped scan."""
        total, cursor = 0, None
        entries = 0
        while True:
            rows, cursor = await self._store.find(self.collection, {"tenant": tenant},
                                                  limit=500, after=cursor, sort=("created", "asc"))
            for r in rows:
                total += int(r.get("amount_minor", 0))
                entries += 1
            if not cursor:
                break
        stored = await self.balance(tenant)
        return {"tenant": tenant, "consistent": stored == total, "stored": stored,
                "derived": total, "entries": entries}


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
        """Metered usage for one subscription in one period.

        Runs at invoice time for every subscription. It used to read EVERY usage row of every
        tenant, once per subscription per metric — quadratic in customers. Now the subscription
        and the metric are pushed into the datastore and only that series is paged; the period
        window is a range the equality filter cannot express, so it is applied here.
        """
        total, cursor = 0, None
        while True:
            rows, cursor = await self._store.find(
                self.collection, {"subscription_id": subscription_id, "metric": metric},
                limit=_SWEEP_PAGE, after=cursor, sort=("created", "asc"))
            for r in rows:
                if start <= float(r.get("ts", 0)) < end:
                    total += int(r.get("quantity", 0))
            if not cursor:
                break
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
        """The settlements recorded against one invoice, oldest first."""
        rows, _ = await self._store.find(self.collection, {"invoice_id": invoice_id},
                                         limit=limit, sort=("created", "asc"))
        return rows
