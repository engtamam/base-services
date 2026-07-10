"""DSAR (hold blocks erasure, completeness, verification, SLA) + RoPA + breach 72h clock."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.orchestrator.null import NullOrchestrator
from app.core.compliance_service import ComplianceService
from app.core.dsar_service import DsarService
from app.core.models import COMPLETED, IN_PROGRESS, REFUSED
from app.core.stores import BreachStore, DsarStore, HoldStore, RopaStore, TargetStore

CFG = dict(sla_days=30, verification_ttl=86_400, list_limit=100, breach_hours=72,
           residency_default="SA")


def run(c):
    return asyncio.run(c)


class RecordingOrch:
    driver = "rec"

    def __init__(self, ok=True):
        self.sent = []
        self.ok = ok

    async def dispatch(self, **k):
        self.sent.append(k)
        return self.ok


def _env(clock=None, orch=None):
    st = MemoryDataStore()
    clk = clock or ManualClock(1_000_000.0)
    comp = ComplianceService(holds=HoldStore(st), ropa=RopaStore(st), breaches=BreachStore(st),
                             targets=TargetStore(st), clock=clk, config=CFG)
    ds = DsarService(dsars=DsarStore(st), holds=HoldStore(st), targets=TargetStore(st),
                     orchestrator=orch or RecordingOrch(), clock=clk, config=CFG)
    return comp, ds, clk


def _targets(comp):
    run(comp.register_target(tenant="t1", service="identity", category="profile"))
    run(comp.register_target(tenant="t1", service="billing", category="invoices"))


# ── legal hold beats erasure (THE footgun) ────────────────────────────────────
def test_legal_hold_blocks_erasure():
    comp, ds, _ = _env()
    _targets(comp)
    hold = run(comp.place_hold(tenant="t1", subject="u1", reason="litigation"))
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    res = run(ds.process(d.id))
    assert res["ok"] is False and res["reason"] == "LEGAL_HOLD"
    assert run(ds.get(d.id)).status == REFUSED
    assert res["hold_id"] == hold.id


def test_erasure_proceeds_after_hold_released():
    comp, ds, _ = _env()
    _targets(comp)
    hold = run(comp.place_hold(tenant="t1", subject="u1", reason="x"))
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    run(ds.process(d.id))
    run(comp.release_hold(hold.id))
    d2, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    res = run(ds.process(d2.id))
    assert res["ok"] and res["status"] == IN_PROGRESS


def test_hold_on_another_subject_does_not_block():
    comp, ds, _ = _env()
    _targets(comp)
    run(comp.place_hold(tenant="t1", subject="someone-else", reason="x"))
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    assert run(ds.process(d.id))["ok"] is True


# ── completeness is derived, never declared ───────────────────────────────────
def test_erasure_completes_only_when_every_target_acks():
    comp, ds, _ = _env()
    _targets(comp)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    run(ds.process(d.id))
    a1 = run(ds.ack_target(d.id, "identity", "profile"))
    assert a1["status"] == IN_PROGRESS and a1["remaining"] == ["billing:invoices"]
    a2 = run(ds.ack_target(d.id, "billing", "invoices"))
    assert a2["status"] == COMPLETED and run(ds.get(d.id)).completed_at > 0


def test_ack_is_idempotent_and_unknown_target_rejected():
    comp, ds, _ = _env()
    _targets(comp)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    run(ds.process(d.id))
    run(ds.ack_target(d.id, "identity", "profile"))
    run(ds.ack_target(d.id, "identity", "profile"))                # idempotent
    assert run(ds.get(d.id)).status == IN_PROGRESS                 # still one target outstanding
    assert run(ds.ack_target(d.id, "ghost", "x"))["reason"] == "UNKNOWN_TARGET"


def test_process_refuses_when_no_targets_registered():
    _, ds, _ = _env()
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    assert run(ds.process(d.id))["reason"] == "NO_DATA_TARGETS_REGISTERED"


def test_null_orchestrator_never_claims_dispatch():
    comp, ds, _ = _env(orch=NullOrchestrator())
    _targets(comp)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    res = run(ds.process(d.id))
    assert res["dispatched"] == 0 and res["fully_dispatched"] is False


# ── identity before PII ───────────────────────────────────────────────────────
def test_access_requires_verification():
    comp, ds, _ = _env()
    _targets(comp)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="access"))
    assert run(ds.process(d.id))["reason"] == "NOT_VERIFIED"
    assert run(ds.verify(d.id, "wrong-token"))["reason"] == "BAD_TOKEN"
    assert run(ds.verify(d.id, d.verification_token))["ok"] is True
    assert run(ds.process(d.id))["ok"] is True


def test_verification_token_expires():
    clk = ManualClock(1000.0)
    comp, ds, _ = _env(clock=clk)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="export"))
    clk.advance(CFG["verification_ttl"] + 10)
    assert run(ds.verify(d.id, d.verification_token))["reason"] == "TOKEN_EXPIRED"


def test_erasure_does_not_require_verification():
    # erasure releases no PII — requiring a token would let an attacker BLOCK an erasure
    comp, ds, _ = _env()
    _targets(comp)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    assert run(ds.process(d.id))["ok"] is True


# ── idempotency + SLA ──────────────────────────────────────────────────────────
def test_duplicate_open_request_returns_the_same_one():
    _, ds, _ = _env()
    a, c1 = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    b, c2 = run(ds.create(tenant="t1", subject="u1", type="erasure"))
    assert c1 is True and c2 is False and a.id == b.id


def test_sla_deadline_and_overdue_flag_utc():
    clk = ManualClock(1000.0)
    _, ds, _ = _env(clock=clk)
    d, _ = run(ds.create(tenant="t1", subject="u1", type="access"))
    assert d.deadline_at == 1000.0 + 30 * 86400
    assert d.overdue(clk.now()) is False
    clk.advance(31 * 86400)
    assert run(ds.get(d.id)).overdue(clk.now()) is True


def test_public_view_hides_verification_token():
    _, ds, _ = _env()
    d, _ = run(ds.create(tenant="t1", subject="u1", type="access"))
    assert "verification_token" not in d.public()


# ── RoPA ───────────────────────────────────────────────────────────────────────
def test_ropa_rejects_invalid_lawful_basis():
    comp, _, _ = _env()
    with pytest.raises(ValueError):
        run(comp.create_activity(tenant="t1", name="x", purpose="p", lawful_basis="vibes",
                                 data_categories=[]))


def test_ropa_defaults_residency():
    comp, _, _ = _env()
    a = run(comp.create_activity(tenant="t1", name="billing", purpose="invoicing",
                                 lawful_basis="contract", data_categories=["financial"]))
    assert a.residency == "SA" and a.cross_border is False


# ── breach 72-hour clock ───────────────────────────────────────────────────────
def test_breach_deadline_and_overdue():
    clk = ManualClock(1000.0)
    comp, _, _ = _env(clock=clk)
    b = run(comp.register_breach(tenant="t1", description="leak", affected_subjects=5))
    assert b.notify_deadline_at == 1000.0 + 72 * 3600
    s = run(comp.breach_status(b.id))
    assert s["hours_remaining"] == 72.0 and s["overdue"] is False
    clk.advance(73 * 3600)
    assert run(comp.breach_status(b.id))["overdue"] is True


def test_notifying_late_is_flagged_and_idempotent():
    clk = ManualClock(1000.0)
    comp, _, _ = _env(clock=clk)
    b = run(comp.register_breach(tenant="t1", description="leak"))
    clk.advance(80 * 3600)
    first = run(comp.notify_breach(b.id))
    assert first["late"] is True
    again = run(comp.notify_breach(b.id))
    assert "already_notified_at" in again
    assert run(comp.breach_status(b.id))["overdue"] is False   # notified → clock stops


def test_breach_notified_in_time_is_not_late():
    clk = ManualClock(1000.0)
    comp, _, _ = _env(clock=clk)
    b = run(comp.register_breach(tenant="t1", description="leak"))
    clk.advance(10 * 3600)
    assert run(comp.notify_breach(b.id))["late"] is False


# ── legal hold helpers ─────────────────────────────────────────────────────────
def test_is_held_reflects_place_and_release():
    comp, _, _ = _env()
    assert run(comp.is_held("t1", "u1")) is False
    h = run(comp.place_hold(tenant="t1", subject="u1", reason="x"))
    assert run(comp.is_held("t1", "u1")) is True
    run(comp.release_hold(h.id))
    assert run(comp.is_held("t1", "u1")) is False
