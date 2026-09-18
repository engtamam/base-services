"""M6 — the background reconciliation / expiry sweep.

Proves the safety net does its job and nothing more:
  * an overdue PENDING is expired;
  * an old PENDING whose webhook never arrived is reconciled to COMPLETED via the idempotent seam;
  * a young PENDING is left alone;
  * a per-row gateway error is swallowed without aborting the pass;
  * the sweep pages the datastore (works beyond one page);
  * the periodic loop is cancellable;
  * OFF by default — the lifespan builds a sweep only when the flag is set.

Uses asyncio.run (the service's test convention — no pytest-asyncio plugin).
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.event_bus.memory import MemoryEventBus
from app.core.payment_requests import COMPLETED, EXPIRED, PaymentRequestStore
from app.core.payments import PaymentService
from app.core.reconcile_sweep import ReconcileSweep, run_sweep_once
from app.ports.payment_gateway import PENDING, SUCCEEDED, GatewayUnavailable, PaymentOutcome


def run(c):
    return asyncio.run(c)


class _FakeGateway:
    """fetch_status reports a verified, amount-matching success — the settlement a missed webhook
    would have carried. Optionally raises, to prove per-row errors are isolated."""

    driver = "fake"

    def __init__(self, *, amount_minor: int, currency: str, raises: bool = False) -> None:
        self._amount = amount_minor
        self._currency = currency
        self._raises = raises

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        if self._raises:
            raise GatewayUnavailable("gateway down")
        return PaymentOutcome(status=SUCCEEDED, provider=self.driver, provider_ref=provider_ref,
                              amount_minor=self._amount, currency=self._currency, verified=True,
                              event_id="evt-1")


class _FakeGateways:
    def __init__(self, gw) -> None:
        self._gw = gw

    async def gateway_for(self, tenant: str):
        return self._gw


def _service(requests, gateway) -> PaymentService:
    return PaymentService(
        requests=requests, gateways=_FakeGateways(gateway), event_bus=MemoryEventBus(),
        redirect_base_url="", allowed_hosts=frozenset(), link_ttl_seconds=100_000)


async def _seed(requests, *, tenant, reference, amount, ttl):
    return await requests.create(
        tenant=tenant, reference=reference, amount_minor=amount, currency="SAR",
        provider="fake", provider_ref=f"fake_{reference}", redirect_url="https://x.test/p",
        ttl_seconds=ttl)


def test_sweep_expires_overdue_and_reconciles_old():
    async def scenario():
        clock = {"t": 1000.0}
        now = lambda: clock["t"]  # noqa: E731
        store = MemoryDataStore()
        requests = PaymentRequestStore(store, now=now)

        overdue = await _seed(requests, tenant="t1", reference="ord-exp", amount=500, ttl=10)   # exp@1010
        old = await _seed(requests, tenant="t1", reference="ord-old", amount=700, ttl=100_000)  # not expired
        service = _service(requests, _FakeGateway(amount_minor=700, currency="SAR"))

        clock["t"] = 2000.0  # past the overdue TTL, and both created_at=1000 are >min_age old
        stats = await run_sweep_once(datastore=store, requests=requests, service=service,
                                     min_age_seconds=900, page_size=200, now=now)

        assert (await requests.get(overdue["id"]))["status"] == EXPIRED
        assert (await requests.get(old["id"]))["status"] == COMPLETED
        assert stats.expired == 1
        assert stats.reconciled == 1
        assert stats.errors == 0

    run(scenario())


def test_sweep_leaves_young_pending_untouched():
    async def scenario():
        clock = {"t": 1000.0}
        now = lambda: clock["t"]  # noqa: E731
        store = MemoryDataStore()
        requests = PaymentRequestStore(store, now=now)
        young = await _seed(requests, tenant="t1", reference="ord-young", amount=300, ttl=100_000)
        service = _service(requests, _FakeGateway(amount_minor=300, currency="SAR"))

        clock["t"] = 1100.0  # only 100s old — below min_age, not expired
        stats = await run_sweep_once(datastore=store, requests=requests, service=service,
                                     min_age_seconds=900, page_size=200, now=now)

        assert (await requests.get(young["id"]))["status"] == PENDING
        assert stats.reconciled == 0 and stats.expired == 0

    run(scenario())


def test_sweep_isolates_a_failing_row():
    async def scenario():
        clock = {"t": 1000.0}
        now = lambda: clock["t"]  # noqa: E731
        store = MemoryDataStore()
        requests = PaymentRequestStore(store, now=now)
        a = await _seed(requests, tenant="t1", reference="ord-a", amount=100, ttl=100_000)
        b = await _seed(requests, tenant="t2", reference="ord-b", amount=100, ttl=100_000)
        service = _service(requests, _FakeGateway(amount_minor=100, currency="SAR", raises=True))

        clock["t"] = 2000.0
        stats = await run_sweep_once(datastore=store, requests=requests, service=service,
                                     min_age_seconds=900, page_size=200, now=now)

        # Both rows tried, both errored, neither aborted the pass, neither completed.
        assert stats.scanned == 2
        assert stats.errors == 2
        assert (await requests.get(a["id"]))["status"] == PENDING
        assert (await requests.get(b["id"]))["status"] == PENDING

    run(scenario())


def test_sweep_pages_beyond_one_page():
    async def scenario():
        clock = {"t": 1000.0}
        now = lambda: clock["t"]  # noqa: E731
        store = MemoryDataStore()
        requests = PaymentRequestStore(store, now=now)
        for i in range(5):
            await _seed(requests, tenant="t1", reference=f"ord-{i}", amount=100, ttl=100_000)
        service = _service(requests, _FakeGateway(amount_minor=100, currency="SAR"))

        clock["t"] = 2000.0
        stats = await run_sweep_once(datastore=store, requests=requests, service=service,
                                     min_age_seconds=900, page_size=2, now=now)  # forces 3 pages

        assert stats.scanned == 5
        assert stats.reconciled == 5

    run(scenario())


def test_periodic_loop_starts_and_is_cancellable():
    async def scenario():
        store = MemoryDataStore()
        requests = PaymentRequestStore(store)
        service = _service(requests, _FakeGateway(amount_minor=1, currency="SAR"))
        sweep = ReconcileSweep(datastore=store, requests=requests, service=service,
                               interval_seconds=1, min_age_seconds=900, page_size=10)
        sweep.start()
        await asyncio.sleep(0.05)   # let at least one pass run
        await sweep.stop()          # must cancel cleanly (no hang, no raise)
        assert sweep._task is None

    run(scenario())


def test_sweep_off_by_default_in_lifespan(monkeypatch):
    """The gate: with the flag unset (default False), the lifespan builds NO sweep."""
    import app.core.reconcile_sweep as rs
    from fastapi.testclient import TestClient

    from app.main import app

    calls = []
    monkeypatch.setattr(rs, "build_sweep", lambda *a, **k: calls.append(1))
    with TestClient(app):   # triggers lifespan startup/shutdown
        pass
    assert calls == []


def test_sweep_enabled_is_started_and_stopped(monkeypatch):
    """With the flag on, the lifespan builds the sweep, starts it, and stops it on shutdown."""
    import app.config as cfg
    import app.core.reconcile_sweep as rs
    from fastapi.testclient import TestClient

    from app.main import create_app

    events: list[str] = []

    class _FakeSweep:
        def start(self):
            events.append("start")

        async def stop(self):
            events.append("stop")

    monkeypatch.setenv("PAYMENTS_RECONCILE_SWEEP_ENABLED", "true")
    cfg.get_settings.cache_clear()
    monkeypatch.setattr(rs, "build_sweep", lambda *a, **k: (events.append("build"), _FakeSweep())[1])
    try:
        app2 = create_app()
        with TestClient(app2):
            pass
    finally:
        cfg.get_settings.cache_clear()

    assert events == ["build", "start", "stop"]
