"""Off-gateway settlement (Phase 27).

The defects this file exists to prevent:

  * A globally-unique reference REJECTS A REAL PAYMENT the day two banks mint the same
    string. The identity of a payment is (method, issuer, reference).
  * `(method, reference)` collides for the same reason — two banks, both `bank_transfer`.
  * A duplicate error that names the owner turns settlement into an ORACLE for other
    tenants' transfers.
  * `paid_at = now()` files the tax invoice in the wrong period.
  * A partial payment that finalizes the e-invoice sends ZATCA an invoice per tranche.
  * Two settlements racing on one invoice, both closing it, both writing a ledger row.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core import settlement as S
from app.core.billing_service import BillingService
from app.core.models import Invoice, new_id
from app.core.settlement import SettlementError
from app.core.stores import (CouponStore, IdempotencyStore, InvoiceStore, LedgerStore,
                             PaymentStore, PlanStore, SettlementStore, SubscriptionStore,
                             UsageStore)

METHODS = {"bank_transfer": True, "cheque": True, "cash": False}
NOW = time.time()


def run(c):
    return asyncio.run(c)


def days_ago(n: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=n)).isoformat()


class _Einvoicer:
    def __init__(self):
        self.calls = 0

    def finalize(self, *, tenant, invoice):
        self.calls += 1
        return {"uuid": f"e-{invoice['number']}"}


def _svc(store=None, **over):
    st = store or MemoryDataStore()
    cfg = dict(currency="SAR", trial_days=0, vat_rate="0.15", reverse_charge=False,
               grandfather=True, dunning_base_hours=24, dunning_max_attempts=4, grace_days=7,
               list_limit=100, limits_cache_ttl=300, settlement_methods=METHODS,
               settlement_allow_partial=True, settlement_tolerance_minor=0,
               settlement_max_backdate_days=90, document_mode="tax_invoice",
               # Phase 42: required by EntitlementResolver.
               billable_statuses=("active", "trialing", "past_due"),
               max_subscriptions=200, merge_strategies={}, rank_orders={})
    cfg.update(over)
    einv = _Einvoicer()
    svc = BillingService(plans=PlanStore(st), coupons=CouponStore(st), subs=SubscriptionStore(st),
                         invoices=InvoiceStore(st), payments=PaymentStore(st), usage=UsageStore(st),
                         ledger=LedgerStore(st), idempo=IdempotencyStore(st), gateway=None,
                         tax=None, einvoicer=einv, pdf=None, config=cfg,
                         settlements=SettlementStore(st))
    return svc, einv


async def _invoice(svc, total=100_000, currency="SAR", tenant="acme"):
    """Issued 30 days ago — an enterprise pays 30-60 days after the invoice, never before."""
    inv = Invoice(id=new_id("inv_"), tenant=tenant, number="INV-1", total_minor=total,
                  currency=currency, status="open", created=days_ago(30))
    await svc._invoices.save(inv)
    return inv


def _settle(svc, inv, **kw):
    kw.setdefault("method", "bank_transfer")
    kw.setdefault("issuer", "SABB")
    kw.setdefault("reference", "MT103-1")
    kw.setdefault("amount_minor", inv.total_minor)
    kw.setdefault("paid_at", NOW)
    return run(svc.settle(invoice_id=inv.id, **kw))


# ── THE identity of a payment ───────────────────────────────────────────────
def test_the_same_reference_from_two_different_issuers_both_settle():
    """A bank reference is unique per BANK, not per planet. Global uniqueness would have
    rejected a real, valid payment — the defect the requesting team caught."""
    svc, _ = _svc()
    a = run(_invoice(svc))
    b = run(_invoice(svc))
    _settle(svc, a, issuer="SABB", reference="REF-1")
    _settle(svc, b, issuer="ALRAJHI", reference="REF-1")     # different bank, same string
    assert run(svc._invoices.fetch(a.id)).status == "paid"
    assert run(svc._invoices.fetch(b.id)).status == "paid"


def test_method_and_reference_alone_would_collide():
    """`(method, reference)` is not an identity: two banks, both bank_transfer, same string."""
    assert S.identity("t", "bank_transfer", "SABB", "R") != \
           S.identity("t", "bank_transfer", "ALRAJHI", "R")


def test_one_transfer_cannot_settle_two_invoices():
    svc, _ = _svc()
    a = run(_invoice(svc))
    b = run(_invoice(svc))
    _settle(svc, a, reference="WIRE-9")
    with pytest.raises(SettlementError, match="SETTLEMENT_ALREADY_RECORDED"):
        _settle(svc, b, reference="WIRE-9")


def test_the_duplicate_error_is_not_an_oracle():
    """It must never reveal WHO holds the original settlement, or against which invoice."""
    svc, _ = _svc()
    a = run(_invoice(svc, tenant="victim"))
    b = run(_invoice(svc, tenant="attacker"))
    _settle(svc, a, reference="PROBE")
    with pytest.raises(SettlementError) as exc:
        _settle(svc, b, reference="PROBE")
    message = str(exc.value)
    assert message == "SETTLEMENT_ALREADY_RECORDED"
    assert "victim" not in message and a.id not in message


def test_a_method_without_an_issuer_is_scoped_to_the_tenant():
    """Cash has no issuer. Receipt 0001 in two tenants is two payments, correctly."""
    svc, _ = _svc()
    a = run(_invoice(svc, tenant="t1"))
    b = run(_invoice(svc, tenant="t2"))
    r1 = _settle(svc, a, method="cash", issuer="", reference="RCPT-1")
    r2 = _settle(svc, b, method="cash", issuer="", reference="RCPT-1")
    assert r1["settlement"]["issuer"] == "t1"
    assert r2["settlement"]["issuer"] == "t2"


def test_an_issuer_is_required_where_the_method_says_so():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="ISSUER_REQUIRED_FOR_METHOD"):
        _settle(svc, inv, issuer="", reference="X")


def test_an_issuer_is_refused_where_the_method_has_none():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="ISSUER_NOT_ALLOWED_FOR_METHOD"):
        _settle(svc, inv, method="cash", issuer="SABB", reference="X")


def test_an_unknown_method_fails_closed_without_echoing_the_allowlist():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError) as exc:
        _settle(svc, inv, method="crypto")
    assert str(exc.value) == "UNKNOWN_SETTLEMENT_METHOD"
    assert "bank_transfer" not in str(exc.value)      # config is not caller business


# ── paid_at is the bank's value date ────────────────────────────────────────
def test_paid_at_is_the_banks_value_date_not_now():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    value_date = NOW - 3 * 86400
    r = _settle(svc, inv, paid_at=value_date)
    recorded = dt.datetime.fromisoformat(r["settlement"]["paid_at"]).timestamp()
    assert abs(recorded - value_date) < 2
    # and the invoice carries it, so the tax period is right
    assert run(svc._invoices.fetch(inv.id)).paid_at == r["settlement"]["paid_at"]


def test_a_future_value_date_is_refused():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="PAID_AT_IN_THE_FUTURE"):
        _settle(svc, inv, paid_at=NOW + 86400)


def test_a_value_date_before_the_invoice_is_refused():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="PAID_AT_BEFORE_INVOICE"):
        _settle(svc, inv, paid_at=NOW - 60 * 86400)      # invoice issued 30 days ago


def test_backdating_is_bounded_so_a_closed_tax_period_is_not_rewritten():
    svc, _ = _svc(settlement_max_backdate_days=10)
    inv = run(_invoice(svc))
    inv.created = days_ago(400)                          # an old invoice
    run(svc._invoices.save(inv))
    with pytest.raises(SettlementError, match="PAID_AT_TOO_OLD"):
        _settle(svc, inv, paid_at=NOW - 30 * 86400)


# ── amounts, partials, tolerance ────────────────────────────────────────────
def test_a_partial_settlement_does_not_finalize_the_einvoice():
    """ZATCA gets one invoice, once — never one per tranche."""
    svc, einv = _svc()
    inv = run(_invoice(svc))
    r1 = _settle(svc, inv, reference="T1", amount_minor=40_000)
    assert r1["invoice_status"] == "partially_paid"
    assert r1["einvoice_finalized"] is False
    assert einv.calls == 0

    r2 = _settle(svc, inv, reference="T2", amount_minor=60_000)
    assert r2["invoice_status"] == "paid" and r2["remaining_minor"] == 0
    assert einv.calls == 1
    assert len(run(svc._ledger.list("acme"))) == 1       # exactly one charge row


def test_a_partial_settlement_leaves_dunning_chasing_the_remainder():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    _settle(svc, inv, amount_minor=40_000)
    # `partially_paid` is DERIVED from the atomic counter; the stored status stays `open`, so
    # dunning_sweep (which skips only `paid`) keeps chasing the remainder.
    assert run(svc.invoice_state(inv.id)) == "partially_paid"
    assert run(svc._invoices.fetch(inv.id)).status == "open"


def test_partial_can_be_forbidden_by_config():
    svc, _ = _svc(settlement_allow_partial=False)
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="PARTIAL_SETTLEMENT_NOT_ALLOWED"):
        _settle(svc, inv, amount_minor=40_000)


def test_a_bank_fee_within_tolerance_closes_the_invoice():
    """A correspondent bank takes its fee out of the wire. Without tolerance every
    enterprise invoice on the platform sits `open` over 20 halalas."""
    svc, einv = _svc(settlement_tolerance_minor=50)
    inv = run(_invoice(svc))
    r = _settle(svc, inv, amount_minor=99_980)
    assert r["invoice_status"] == "paid" and einv.calls == 1


def test_overpayment_is_refused_rather_than_inventing_a_credit():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="AMOUNT_EXCEEDS_OUTSTANDING"):
        _settle(svc, inv, amount_minor=200_000)


def test_a_non_positive_amount_is_refused():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="AMOUNT_MUST_BE_POSITIVE"):
        _settle(svc, inv, amount_minor=0)


def test_a_currency_mismatch_is_refused_never_converted():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    with pytest.raises(SettlementError, match="CURRENCY_MISMATCH"):
        _settle(svc, inv, currency="USD")


def test_an_already_paid_invoice_cannot_be_settled_again():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    _settle(svc, inv, reference="A")
    with pytest.raises(SettlementError, match="INVOICE_NOT_SETTLEABLE"):
        _settle(svc, inv, reference="B")


def test_a_draft_or_void_invoice_cannot_be_settled():
    for status in ("draft", "void", "uncollectible"):
        svc, _ = _svc()
        inv = run(_invoice(svc))
        inv.status = status
        run(svc._invoices.save(inv))
        with pytest.raises(SettlementError, match="INVOICE_NOT_SETTLEABLE"):
            _settle(svc, inv)


def test_settling_another_tenants_invoice_is_a_not_found():
    svc, _ = _svc()
    inv = run(_invoice(svc, tenant="victim"))
    with pytest.raises(SettlementError, match="INVOICE_NOT_FOUND"):
        run(svc.settle(invoice_id=inv.id, tenant="attacker", method="bank_transfer",
                       issuer="S", reference="R", amount_minor=100_000, paid_at=NOW))


# ── concurrency: the reason this is not a hot-path problem ──────────────────
def test_fifty_concurrent_settlements_close_the_invoice_exactly_once():
    svc, einv = _svc()
    inv = run(_invoice(svc))

    async def race():
        return await asyncio.gather(
            *[svc.settle(invoice_id=inv.id, method="bank_transfer", issuer="SABB",
                         reference=f"R{i}", amount_minor=inv.total_minor, paid_at=NOW)
              for i in range(50)], return_exceptions=True)

    results = asyncio.run(race())
    winners = [r for r in results if not isinstance(r, Exception)]
    final = run(svc._invoices.fetch(inv.id))

    assert len(winners) == 1, f"{len(winners)} settlements closed the same invoice"
    assert final.status == "paid"
    assert run(svc.settled_minor(inv.id)) == inv.total_minor
    assert einv.calls == 1, "the e-invoice was finalized more than once"
    assert len(run(svc._ledger.list("acme"))) == 1, "more than one ledger row"


def test_concurrent_tranches_never_oversettle():
    svc, _ = _svc()
    inv = run(_invoice(svc, total=100_000))

    async def race():
        return await asyncio.gather(
            *[svc.settle(invoice_id=inv.id, method="bank_transfer", issuer="SABB",
                         reference=f"T{i}", amount_minor=30_000, paid_at=NOW)
              for i in range(10)], return_exceptions=True)

    results = asyncio.run(race())
    ok = [r for r in results if not isinstance(r, Exception)]
    assert len(ok) == 3, f"{len(ok)} x 30000 settled a 100000 invoice"
    # The COUNTER is the truth. Writing settled_minor onto the invoice row from each tranche
    # was a lost update — three writers, last one wins with a stale value. Postgres caught it.
    assert run(svc.settled_minor(inv.id)) == 90_000
    assert run(svc.invoice_state(inv.id)) == "partially_paid"


def test_a_failed_settlement_releases_its_identity_claim():
    """Otherwise a transient failure would burn the bank reference forever."""
    svc, _ = _svc()
    inv = run(_invoice(svc, total=100_000))
    _settle(svc, inv, reference="A", amount_minor=100_000)   # closes it

    # The claim for 'B' is taken, then the amount check inside the store rejects it.
    other = run(_invoice(svc, total=100_000))
    _settle(svc, other, reference="B", amount_minor=60_000)
    with pytest.raises(SettlementError):
        _settle(svc, other, reference="C", amount_minor=60_000)   # exceeds outstanding
    # 'C' was never recorded, so it can still be used for its real amount.
    r = _settle(svc, other, reference="C", amount_minor=40_000)
    assert r["invoice_status"] == "paid"


# ── reversal ────────────────────────────────────────────────────────────────
def test_a_reversal_keeps_history_and_issues_a_credit_note():
    """A cleared ZATCA invoice cannot be un-cleared. Never delete; issue a credit note."""
    svc, _ = _svc()
    inv = run(_invoice(svc))
    r = _settle(svc, inv, reference="RECALL")
    sid = r["settlement"]["id"]

    rv = run(svc.reverse_settlement(settlement_id=sid, reason="recalled", actor="fin"))
    assert rv["credit_note"]["amount_minor"] == inv.total_minor
    assert rv["invoice_status"] == "open"

    kept = run(svc._settlements.fetch(sid))
    assert kept is not None and kept.status == "reversed" and kept.reversed_reason == "recalled"
    rows = run(svc._ledger.list("acme"))
    assert len(rows) == 2 and {r["kind"] for r in rows} == {"charge", "refund"}


def test_a_reversal_does_not_free_the_reference_for_reuse():
    """The transfer really did happen once. Reusing its reference would double-count it."""
    svc, _ = _svc()
    inv = run(_invoice(svc))
    sid = _settle(svc, inv, reference="RECALL")["settlement"]["id"]
    run(svc.reverse_settlement(settlement_id=sid))
    with pytest.raises(SettlementError, match="SETTLEMENT_ALREADY_RECORDED"):
        _settle(svc, inv, reference="RECALL")


def test_reversing_twice_is_idempotent():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    sid = _settle(svc, inv, reference="R")["settlement"]["id"]
    run(svc.reverse_settlement(settlement_id=sid))
    again = run(svc.reverse_settlement(settlement_id=sid))
    assert again["idempotent"] is True
    assert len(run(svc._ledger.list("acme"))) == 2      # no second credit note


def test_reversing_one_tranche_leaves_the_invoice_partially_paid():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    a = _settle(svc, inv, reference="T1", amount_minor=40_000)
    _settle(svc, inv, reference="T2", amount_minor=60_000)
    rv = run(svc.reverse_settlement(settlement_id=a["settlement"]["id"]))
    assert rv["invoice_status"] == "partially_paid"
    assert rv["settled_minor"] == 60_000
    assert run(svc.settled_minor(inv.id)) == 60_000


def test_reversing_an_unknown_settlement_is_not_found():
    svc, _ = _svc()
    with pytest.raises(SettlementError, match="SETTLEMENT_NOT_FOUND"):
        run(svc.reverse_settlement(settlement_id="stl_nope"))


# ── the audit trail ─────────────────────────────────────────────────────────
def test_the_settlement_records_who_decided_and_the_proof():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    r = _settle(svc, inv, actor="finance@acme.com", proof_file_id="file_123", note="PO-4471")
    s = r["settlement"]
    assert s["actor"] == "finance@acme.com"      # a person, not a service token
    assert s["proof_file_id"] == "file_123"      # a file-service id, never a blob
    assert s["note"] == "PO-4471"


def test_settlements_are_listed_for_an_invoice():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    _settle(svc, inv, reference="T1", amount_minor=40_000)
    _settle(svc, inv, reference="T2", amount_minor=60_000)
    rows = run(svc.settlements_for(inv.id))
    assert len(rows) == 2 and {r["reference"] for r in rows} == {"T1", "T2"}


# ── the lost update Postgres exposed ────────────────────────────────────────
def test_the_counter_is_the_truth_not_the_invoice_row():
    """Three tranches each writing `settled_minor` onto the invoice row is a lost update:
    the last writer wins with a stale value. Only the counter is authoritative."""
    svc, _ = _svc()
    inv = run(_invoice(svc))
    _settle(svc, inv, reference="A", amount_minor=30_000)
    _settle(svc, inv, reference="B", amount_minor=30_000)
    _settle(svc, inv, reference="C", amount_minor=30_000)
    assert run(svc.settled_minor(inv.id)) == 90_000


def test_a_late_partial_settlement_cannot_downgrade_a_paid_invoice():
    """`paid` is written once, by the claim winner. Nothing else touches the invoice row."""
    svc, einv = _svc(settlement_tolerance_minor=10_000)
    inv = run(_invoice(svc))
    r = _settle(svc, inv, reference="NEARLY", amount_minor=95_000)   # closes within tolerance
    assert r["invoice_status"] == "paid" and einv.calls == 1
    with pytest.raises(SettlementError, match="INVOICE_NOT_SETTLEABLE"):
        _settle(svc, inv, reference="LATE", amount_minor=5_000)
    assert run(svc._invoices.fetch(inv.id)).status == "paid"


def test_only_one_settlement_finalizes_the_einvoice_even_when_several_can_close():
    """With a tolerance, more than one settlement can observe `closes`. Exactly one wins."""
    svc, einv = _svc(settlement_tolerance_minor=50_000)
    inv = run(_invoice(svc))

    async def race():
        return await asyncio.gather(
            *[svc.settle(invoice_id=inv.id, method="bank_transfer", issuer="SABB",
                         reference=f"C{i}", amount_minor=50_000, paid_at=NOW)
              for i in range(2)], return_exceptions=True)

    asyncio.run(race())
    assert einv.calls == 1, "the e-invoice was finalized more than once"
    assert len(run(svc._ledger.list("acme"))) == 1


# ── their explicit requirement: "still idempotent on retry" ─────────────────
def test_retrying_the_same_triple_on_the_same_invoice_is_idempotent():
    """A caller may not know whether their first request landed. A retry must not
    double-settle, and must not be refused either."""
    svc, einv = _svc()
    inv = run(_invoice(svc))
    first = _settle(svc, inv, reference="RETRY", amount_minor=40_000)
    again = _settle(svc, inv, reference="RETRY", amount_minor=40_000)

    assert again["idempotent"] is True
    assert again["settlement"]["id"] == first["settlement"]["id"]
    assert run(svc.settled_minor(inv.id)) == 40_000     # charged exactly once
    assert einv.calls == 0


def test_a_retry_after_the_invoice_closed_returns_the_original_not_an_error():
    svc, _ = _svc()
    inv = run(_invoice(svc))
    first = _settle(svc, inv, reference="ONCE")
    again = _settle(svc, inv, reference="ONCE")
    assert again["idempotent"] is True
    assert again["invoice_status"] == "paid"
    assert again["settlement"]["id"] == first["settlement"]["id"]


def test_the_same_triple_against_a_different_invoice_is_still_refused():
    """One transfer cannot settle two invoices — the requirement that started this."""
    svc, _ = _svc()
    a = run(_invoice(svc))
    b = run(_invoice(svc))
    _settle(svc, a, reference="ONE-WIRE")
    with pytest.raises(SettlementError, match="SETTLEMENT_ALREADY_RECORDED"):
        _settle(svc, b, reference="ONE-WIRE")
