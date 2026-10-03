"""Non-security MEDIUM remediation (payments-service):
  006  active_by_reference finds the live row even behind many terminal rows for a reused reference.
  009  fetch_status raises GatewayUnavailable on a SUCCEEDED status with an unparseable/zero amount.
  012  MerchantGateways._config drops a stored gateway base that fails https/public validation (read-time).
  019  the reconcile sweep processes a page's rows with bounded concurrency (overlapping, not serial).
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.payment_requests import COMPLETED, PaymentRequestStore, _COLL


def run(c):
    return asyncio.run(c)


# ── 006: the live row is found past many terminal rows ────────────────────────
def test_active_by_reference_finds_live_row_behind_many_terminal_rows():
    st = MemoryDataStore()
    store = PaymentRequestStore(st, now=lambda: 1000.0)
    # 30 terminal (failed) rows for the same reference, all NEWER than the completed one
    run(st.put(_COLL, "live", {"id": "live", "tenant": "t", "reference": "ref1",
                               "status": COMPLETED, "created_at": 1.0, "version": 1}))
    for i in range(30):
        run(st.put(_COLL, f"f{i}", {"id": f"f{i}", "tenant": "t", "reference": "ref1",
                                    "status": "failed", "created_at": 100.0 + i, "version": 1}))
    found = run(store.active_by_reference(tenant="t", reference="ref1"))
    assert found is not None and found["id"] == "live"      # not crowded out by the 30 failed rows


# ── 009: success with unparseable amount → retry ──────────────────────────────
def test_fetch_status_success_with_zero_amount_raises_for_retry(monkeypatch):
    import app.adapters.payment.tuwaiqpay as tp
    from app.ports.payment_gateway import GatewayUnavailable

    gw = tp.TuwaiqPayGateway(api_key="k", extra_secret="s", base="https://gw.example", timeout=5)
    monkeypatch.setattr(gw, "_access_token", lambda: "tok")
    # a SETTLED status but no bill amount
    monkeypatch.setattr(tp, "get_json", lambda *a, **k: (200, {"data": {
        "transactionStatus": "SETTLED", "transactionId": "abc123",
        "bill": {"currency": "SAR"}}}))
    with pytest.raises(GatewayUnavailable):
        gw.fetch_status(provider_ref="abc123", currency="SAR")


# ── 012: a stored invalid base is dropped on read ─────────────────────────────
def test_config_drops_a_non_https_stored_base():
    from app.core.merchant_credentials import (API_BASE, DRIVER, MerchantGateways,
                                               merchant_secret_store)
    st = MemoryDataStore()
    secrets = merchant_secret_store(st, "k" * 32)
    run(secrets.set(tenant="t", name=DRIVER, value="tuwaiqpay"))
    # a non-https base smuggled into the store (bypassing the write-time guard)
    run(secrets.set(tenant="t", name=API_BASE, value="http://attacker.internal/collect"))
    gws = MerchantGateways(secrets)
    cfg = run(gws._config("t"))
    assert cfg.base == ""      # the invalid base is dropped; the driver falls back to its own default


def test_config_keeps_a_valid_https_base():
    from app.core.merchant_credentials import (API_BASE, DRIVER, MerchantGateways,
                                               merchant_secret_store)
    st = MemoryDataStore()
    secrets = merchant_secret_store(st, "k" * 32)
    run(secrets.set(tenant="t", name=DRIVER, value="tuwaiqpay"))
    run(secrets.set(tenant="t", name=API_BASE, value="https://onboarding-prod.tuwaiqpay.com.sa/api/v1"))
    cfg = run(MerchantGateways(secrets)._config("t"))
    assert cfg.base == "https://onboarding-prod.tuwaiqpay.com.sa/api/v1"


# ── 019: the sweep processes rows with bounded concurrency ────────────────────
def test_reconcile_sweep_processes_rows_concurrently():
    from app.core import reconcile_sweep as rs
    from app.core.payment_requests import PENDING

    st = MemoryDataStore()
    now = 10_000.0
    for i in range(4):
        run(st.put(_COLL, f"r{i}", {"id": f"r{i}", "tenant": "t", "reference": f"x{i}",
                                    "status": PENDING, "created_at": 1.0, "version": 1,
                                    "expires_at": now + 1000, "provider_ref": f"p{i}"}))

    order = []

    class _Requests:
        def is_expired(self, row): return False

    class _Service:
        async def reconcile(self, *, tenant, request_id):
            order.append(("start", request_id))
            await asyncio.sleep(0.02)
            order.append(("end", request_id))

    stats = run(rs.run_sweep_once(datastore=st, requests=_Requests(), service=_Service(),
                                  min_age_seconds=0, page_size=50, concurrency=4, now=lambda: now))
    assert stats.reconciled == 4
    # concurrency: a second reconcile starts before the first ends
    first_end = next(i for i, e in enumerate(order) if e[0] == "end")
    starts_before = sum(1 for e in order[:first_end] if e[0] == "start")
    assert starts_before >= 2
