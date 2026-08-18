"""End-to-end: a DSAR reaches `completed` ONLY when every target has actually acked.

Every other test in this phase checks one component. This one checks the claim the whole phase
exists to make — that "she was erased" is true when the system says it. It drives a real DSAR
through the real service, the real dispatcher and the real config parser, with only the network
faked, and asserts the property from the outside.

The three scenarios are the three ways this can go wrong in production:

  * everyone answers            -> completed, with the evidence of what each kept
  * one target is down          -> NOT completed, and the stuck target is named
  * a target answers 202        -> NOT completed until it pushes its ack

A DSAR that reports `completed` in scenarios 2 or 3 is the failure mode that turns a privacy
promise into a lie, so those are the assertions that matter most.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.clock.manual import ManualClock
from app.adapters.datastore.memory import MemoryDataStore
from app.config import Settings
from app.core.dsar_service import DsarService
from app.core.models import COMPLETED, DataTarget, new_id
from app.core.privacy_dispatcher import PrivacyDispatcher
from app.core.privacy_sweeper import PrivacySweeper
from app.core.stores import DsarStore, HoldStore, TargetStore
from app.ports.privacy_transport import ACKED, FAILED, PENDING, EraseOutcome

_TOK = "x" * 40
_TENANT, _SUBJECT = "acme", "alice@example.com"

_SERVICES = ("identity-service", "billing-service", "file-service")


def run(c):
    return asyncio.run(c)


class _Network:
    """Stands in for the wire. Every target answers ACKED unless scripted otherwise."""

    driver = "fake"

    def __init__(self, script=None):
        self.script = script or {}
        self.calls = []

    async def erase(self, *, url, token, request_id, action, tenant, subject, timeout,
                    subject_kind=""):
        self.calls.append(url)
        for name, outcome in self.script.items():
            if name in url:
                return outcome
        return EraseOutcome(status=ACKED, ack={"service": "x", "erased": ["profile"],
                                               "retained": [], "already_absent": False})


def _settings(**kw):
    targets = ",".join(f"{n}=http://{n}:8000" for n in _SERVICES)
    base = dict(INTERNAL_SERVICE_TOKEN=_TOK, PRIVACY_TARGETS=targets,
                PRIVACY_TRANSPORT_DRIVER="http", PRIVACY_MAX_ATTEMPTS=3, PRIVACY_BACKOFF_MS=1)
    base.update(kw)
    return Settings(**base)


async def _world(transport, settings=None):
    """A real datastore, real stores, real service — only the network and the clock are faked.

    The clock is MANUAL on purpose. Retry backoff is measured in milliseconds and time.time() on
    Windows only advances every ~15ms, so a loop of sweeps would run inside a single tick and no
    retry would ever come due — the test would fail while the code was correct. Driving the clock
    explicitly also makes the retry schedule part of what is asserted rather than a race.
    """
    settings = settings or _settings()
    store = MemoryDataStore()
    dsars, holds, targets = DsarStore(store), HoldStore(store), TargetStore(store)
    clock = ManualClock(1_000.0)

    # The data map: registered exactly as an operator would, one row per service.
    for name in _SERVICES:
        await targets.register(DataTarget(id=new_id("tgt_"), tenant=_TENANT, service=name,
                                          category="data"))

    dispatcher = PrivacyDispatcher(dsars=dsars, targets=targets, transport=transport,
                                   settings=settings, clock=clock)
    service = DsarService(dsars=dsars, holds=holds, targets=targets, orchestrator=_NullOrch(),
                          clock=clock, config={"sla_days": 30, "verification_ttl": 3600,
                                               "list_limit": 100},
                          dispatcher=dispatcher)
    return service, dsars, dispatcher, clock


class _NullOrch:
    async def dispatch(self, **kw):
        return True


# ── the happy path ────────────────────────────────────────────────────────────
def test_a_dsar_completes_only_after_every_target_acks():
    async def scenario():
        net = _Network()
        service, dsars, _, clock = await _world(net)
        dsar, _ = await service.create(tenant=_TENANT, subject=_SUBJECT, type="erasure")
        result = await service.process(dsar.id)

        final = await dsars.get(dsar.id)
        assert final.status == COMPLETED
        assert final.is_complete()
        assert result["fanout"]["remaining"] == []
        # every target was actually dialled, at its own configured URL
        assert sorted(net.calls) == sorted(f"http://{n}:8000/internal/privacy/erase"
                                           for n in _SERVICES)
        return final

    final = run(scenario())
    # the ack BODIES are kept: the DSAR can say WHAT each service erased and what it kept
    assert all(state.get("ack") for state in final.target_state.values())


# ── the failure that must never look like success ─────────────────────────────
def test_one_unreachable_target_keeps_the_dsar_incomplete_for_ever():
    async def scenario():
        net = _Network({"billing-service": EraseOutcome(status=FAILED, detail="connection refused")})
        service, dsars, dispatcher, clock = await _world(net)
        dsar, _ = await service.create(tenant=_TENANT, subject=_SUBJECT, type="erasure")
        await service.process(dsar.id)

        # sweep repeatedly, as the CronJob would, until billing exhausts its attempts
        sweeper = PrivacySweeper(dsars=dsars, dispatcher=dispatcher)
        for _ in range(6):
            clock.advance(3600)          # well past any backoff, as a 5-minute cron would be
            summary = await sweeper.run()
        return await dsars.get(dsar.id), summary

    final, summary = run(scenario())
    # THE assertion of this phase: 2 of 3 erased is NOT "done".
    assert final.status != COMPLETED
    assert not final.is_complete()
    unacked = sorted(k for k, acked in final.targets.items() if not acked)
    assert unacked == ["billing-service:data"]
    # the two that DID answer are recorded as done — a failure elsewhere must not lose their acks
    assert final.targets["identity-service:data"] is True
    assert final.targets["file-service:data"] is True


def test_the_stuck_target_is_named_so_an_operator_can_act():
    async def scenario():
        net = _Network({"billing-service": EraseOutcome(status=FAILED, detail="HTTP 500")})
        service, dsars, dispatcher, clock = await _world(net)
        dsar, _ = await service.create(tenant=_TENANT, subject=_SUBJECT, type="erasure")
        await service.process(dsar.id)
        sweeper = PrivacySweeper(dsars=dsars, dispatcher=dispatcher)
        for _ in range(6):
            clock.advance(3600)
            summary = await sweeper.run()
        return summary

    summary = run(scenario())
    stuck = {s["target"] for s in summary["stuck"]}
    assert stuck == {"billing-service:data"}      # a jammed erasure nobody can see is the danger
    assert summary["completed"] == 0


# ── the async target ──────────────────────────────────────────────────────────
def test_a_202_target_holds_the_dsar_open_until_it_pushes_its_ack():
    async def scenario():
        net = _Network({"file-service": EraseOutcome(status=PENDING)})
        service, dsars, _, clock = await _world(net)
        dsar, _ = await service.create(tenant=_TENANT, subject=_SUBJECT, type="erasure")
        await service.process(dsar.id)

        mid = await dsars.get(dsar.id)
        assert mid.status != COMPLETED, "202 is not 'done' — the target is still working"

        # the target finishes and pushes its ack, exactly as the contract says
        await service.ack_target(dsar.id, "file-service", "data",
                                 ack={"erased": ["files:3"], "retained": []})
        return await dsars.get(dsar.id)

    final = run(scenario())
    assert final.status == COMPLETED
    assert final.target_state["file-service:data"]["ack"]["erased"] == ["files:3"]


# ── legal hold still wins, end to end ─────────────────────────────────────────
def test_a_legal_hold_refuses_the_erasure_before_anything_is_dispatched():
    async def scenario():
        from app.core.models import LegalHold
        net = _Network()
        service, dsars, _, clock = await _world(net)
        holds = HoldStore(MemoryDataStore())
        service._holds = holds
        await holds.save(LegalHold(id="h1", tenant=_TENANT, subject=_SUBJECT, reason="litigation",
                                   active=True, placed_at=1.0))
        dsar, _ = await service.create(tenant=_TENANT, subject=_SUBJECT, type="erasure")
        result = await service.process(dsar.id)
        return result, net

    result, net = run(scenario())
    assert result["reason"] == "LEGAL_HOLD"
    assert net.calls == [], "nothing may be dispatched — deleting held data is the violation"
