"""Non-functional guards (per memory `phase-doc-rules`).

Entitlement checks sit in front of every gated action, and feature-flag-service calls one per
flag evaluation. Two halves, OPPOSITE treatment — claiming "zero reads" for both would be a lie:

  * LIMITS are cacheable. A plan changes on upgrade, not per request. Resolving them used to
    scan and sort the whole subscriptions collection, every single time.
  * USAGE is NOT cacheable. Cache a counter and two pods both approve the 26th seat. Every
    `consume()` is one atomic store operation, always.
"""
from __future__ import annotations

import asyncio
import pathlib
import re

from app.adapters.cache.memory import MemoryCache
from app.adapters.datastore.memory import MemoryDataStore
from app.core.billing_service import BillingService
from app.core.models import Plan, Subscription
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SubscriptionStore, UsageStore)
from app.core.usage_service import UsageService

BASE = 1_770_000_000.0


def run(c):
    return asyncio.run(c)


class CountingStore(MemoryDataStore):
    def __init__(self):
        super().__init__()
        self.reads = self.writes = self.scans = self.atomics = 0

    async def get(self, c, k):
        self.reads += 1
        return await super().get(c, k)

    async def put(self, c, k, v):
        self.writes += 1
        return await super().put(c, k, v)

    async def list(self, c, limit=100):
        self.scans += 1
        return await super().list(c, limit)

    async def incr_if_below(self, c, k, d, limit):
        self.atomics += 1
        return await super().incr_if_below(c, k, d, limit)


def _env():
    st, cache = CountingStore(), MemoryCache()
    plans, subs = PlanStore(st, cache), SubscriptionStore(st, cache)
    billing = BillingService(
        plans=plans, coupons=CouponStore(st), subs=subs, invoices=InvoiceStore(st),
        payments=PaymentStore(st), usage=UsageStore(st), ledger=LedgerStore(st),
        idempo=IdempotencyStore(st), gateway=None, tax=None, einvoicer=None, pdf=None,
        cache=cache, config={"grandfather": True, "limits_cache_ttl": 300})

    limits = {"api_calls": {"kind": "counter", "limit": 1_000_000, "period": "month"},
              "seats": 25, "sso": True}
    run(plans.create(Plan(id="plan_1", name="Pro", prices=[], limits=limits)))
    run(subs.save(Subscription(id="sub_1", tenant="t1", plan_id="plan_1", status="active",
                               limits_snapshot=limits, current_period_start=BASE,
                               current_period_end=BASE + 30 * 86400)))
    usage = UsageService(store=st, limits_resolver=billing.limits_resolver,
                         config={"default_period": "month"})
    return billing, usage, st


def test_resolving_limits_after_warm_does_zero_reads_and_zero_scans():
    billing, _, st = _env()
    run(billing._active_limits("t1"))                    # warm
    st.reads = st.writes = st.scans = 0
    for _ in range(200):
        run(billing._active_limits("t1"))
    assert st.scans == 0, f"limit resolution scanned {st.scans} times — it did so per request"
    assert st.reads == 0, f"limit resolution did {st.reads} datastore reads after warm"
    assert st.writes == 0


def test_limit_resolution_cost_does_not_grow_with_the_subscription_table():
    """The old code scanned + sorted every subscription on every entitlement check."""
    def scans_for(n):
        billing, _, st = _env()
        for i in range(n):
            run(billing._subs.save(Subscription(id=f"s{i}", tenant=f"other{i}",
                                                plan_id="plan_1", status="active")))
        run(billing._active_limits("t1"))                # warm
        st.scans = st.reads = 0
        run(billing._active_limits("t1"))
        return st.scans + st.reads

    assert scans_for(5) == scans_for(500) == 0


def test_an_upgrade_invalidates_the_cached_limits_immediately():
    billing, _, _ = _env()
    assert run(billing._active_limits("t1"))["limits"]["seats"] == 25

    sub = run(billing._subs.fetch("sub_1"))
    sub.limits_snapshot = {**sub.limits_snapshot, "seats": 100}
    run(billing._subs.save(sub))                         # save() bumps the generation

    assert run(billing._active_limits("t1"))["limits"]["seats"] == 100, \
        "an upgrade must take effect on the next request, not after the TTL"


def test_a_catalogue_change_invalidates_every_tenant():
    billing, _, _ = _env()
    run(billing._active_limits("t1"))
    run(billing._plans.create(Plan(id="plan_2", name="X", prices=[], limits={})))
    # global generation moved → the cache key changed; no stale read is possible
    gen = run(billing._generation("t1"))
    assert gen.endswith(".2")


def test_consume_is_exactly_one_atomic_operation_and_never_scans():
    _, usage, st = _env()
    run(usage.consume(tenant="t1", key="api_calls"))      # warm the limits cache
    st.reads = st.writes = st.scans = st.atomics = 0
    run(usage.consume(tenant="t1", key="api_calls"))
    assert st.atomics == 1, f"consume used {st.atomics} atomic ops"
    assert st.scans == 0, "consume scanned the datastore"
    assert st.writes == 0, "consume wrote outside the atomic op (a second, racy write)"


def test_consume_cost_does_not_grow_with_the_number_of_counters():
    _, usage, st = _env()
    for i in range(500):
        run(usage.consume(tenant=f"t{i}", key="api_calls"))
    run(usage.consume(tenant="t1", key="api_calls"))
    st.scans = st.atomics = 0
    run(usage.consume(tenant="t1", key="api_calls"))
    assert (st.atomics, st.scans) == (1, 0)


def test_usage_is_never_served_from_a_cache():
    """A cached counter lets two pods both approve the last unit, so UsageService has no cache."""
    import inspect

    assert "cache" not in inspect.signature(UsageService.__init__).parameters, (
        "UsageService accepts a cache — caching a usage counter reintroduces the race that "
        "the atomic increment exists to close"
    )


def test_a_second_reader_sees_a_consume_immediately():
    """Behavioural proof of the above: no read-through cache can hide a fresh increment."""
    billing, usage, st = _env()
    second_reader = UsageService(store=st, limits_resolver=billing.limits_resolver,
                                 config={"default_period": "month"})
    run(usage.consume(tenant="t1", key="api_calls", amount=7))
    assert run(second_reader.current("t1", "api_calls")) == 7


def test_every_settings_field_is_consumed_somewhere():
    from app.config import Settings

    root = pathlib.Path(__file__).resolve().parents[2]
    files = [p for p in root.rglob("*") if p.is_file() and p.suffix in (".py", ".toml", "")
             and "tests" not in p.parts and ".venv" not in p.parts and "__pycache__" not in p.parts]
    lines: list[str] = []
    for p in files:
        try:
            lines.extend(p.read_text(encoding="utf-8").splitlines())
        except (UnicodeDecodeError, OSError):
            continue
    unused = []
    for name in Settings.model_fields:
        decl = re.compile(rf"^\s*{re.escape(name)}\s*:")
        if not [ln for ln in lines if re.search(rf"\b{re.escape(name)}\b", ln) and not decl.match(ln)]:
            unused.append(name)
    assert not unused, f"config knobs declared but never consumed: {unused}"
