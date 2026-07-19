"""The failure policy: what a service does when it acts and cannot record it.

The rule under test — **critical actions fail CLOSED, routine ones fail OPEN and are counted** —
is a product decision, not a technical one, so these tests are written to pin the decision rather
than the implementation. If someone later changes the policy, they should have to change a test
whose name states the trade they are reversing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.audit import AuditUnavailable, Auditor
from app.ports.audit_sink import ACCEPTED, DROPPED, FAILED, AuditReceipt

_CRITICAL = frozenset({"user.suspended", "permission.granted", "data.exported"})


def run(c):
    return asyncio.run(c)


class _Sink:
    driver = "fake"

    def __init__(self, receipt=None, boom=None):
        self.receipt = receipt or AuditReceipt(status=ACCEPTED, id="e1", seq=7, hash="h_1")
        self.boom = boom
        self.events = []

    async def emit(self, event):
        self.events.append(event)
        if self.boom:
            raise self.boom
        return self.receipt


class _Telemetry:
    driver = "fake"

    def __init__(self):
        self.events = []

    def event(self, name, payload):
        self.events.append((name, payload))


def _mk(sink, *, fail_closed=True, telemetry=None):
    return Auditor(sink=sink, service="identity-service", critical_actions=_CRITICAL,
                   fail_closed=fail_closed, telemetry=telemetry)


# ── the happy path ────────────────────────────────────────────────────────────
def test_a_recorded_action_returns_its_place_in_the_chain():
    receipt = run(_mk(_Sink()).record("user.suspended", actor="sub_a", resource="user:u_7"))
    assert receipt.is_recorded and receipt.seq == 7 and receipt.hash == "h_1"


def test_the_event_carries_the_service_and_the_declared_action():
    sink = _Sink()
    run(_mk(sink).record("user.suspended", actor="sub_a", tenant="acme", ip="10.0.0.1",
                         metadata={"reason": "abuse"}))
    event = sink.events[0]
    assert (event.service, event.action, event.actor) == ("identity-service", "user.suspended",
                                                          "sub_a")
    assert event.metadata == {"reason": "abuse"}


# ── the policy ────────────────────────────────────────────────────────────────
def test_a_critical_action_is_refused_when_it_cannot_be_witnessed():
    # THE decision. A permission grant nobody can attribute is indistinguishable from an attacker's,
    # and that is exactly the moment the log has to work.
    auditor = _mk(_Sink(AuditReceipt(status=FAILED, detail="audit-service unreachable")))
    with pytest.raises(AuditUnavailable):
        run(auditor.record("permission.granted", actor="sub_a"))


def test_a_routine_action_proceeds_when_it_cannot_be_witnessed():
    # The same trade lands the other way: taking the platform down to record a display-name change
    # protects nothing.
    receipt = run(_mk(_Sink(AuditReceipt(status=FAILED, detail="down"))).record("profile.updated"))
    assert receipt.is_recorded is False        # honest about the gap
    # and no exception: the action goes ahead


def test_a_dropped_event_is_never_treated_as_recorded():
    # The null sink's answer. "Dropped" is not success, and a critical action must still refuse.
    auditor = _mk(_Sink(AuditReceipt(status=DROPPED, detail="null sink")))
    with pytest.raises(AuditUnavailable):
        run(auditor.record("data.exported"))


def test_every_gap_is_counted_even_when_it_is_tolerated():
    # Failing open SILENTLY would rebuild the exact hole this phase closed, one level down.
    telemetry = _Telemetry()
    run(_mk(_Sink(AuditReceipt(status=FAILED, detail="down")), telemetry=telemetry)
        .record("profile.updated"))
    name, payload = telemetry.events[0]
    assert name == "audit.gap"
    assert payload["action"] == "profile.updated" and payload["critical"] is False


def test_a_critical_gap_is_counted_as_critical():
    telemetry = _Telemetry()
    auditor = _mk(_Sink(AuditReceipt(status=FAILED, detail="down")), telemetry=telemetry)
    with pytest.raises(AuditUnavailable):
        run(auditor.record("user.suspended"))
    assert telemetry.events[0][1]["critical"] is True


def test_fail_open_is_available_but_deliberate():
    """An operator CAN trade evidence for availability during an audit outage. It is config so the
    choice can be made, and documented so it is never made by accident."""
    auditor = _mk(_Sink(AuditReceipt(status=FAILED, detail="down")), fail_closed=False)
    receipt = run(auditor.record("user.suspended"))
    assert receipt.is_recorded is False


def test_a_sink_that_raises_cannot_take_down_the_action_it_was_witnessing():
    # The port forbids raising, but a sink is ordinary code. A routine action must survive a bug in
    # the logging path.
    receipt = run(_mk(_Sink(boom=RuntimeError("bug in the sink"))).record("profile.updated"))
    assert receipt.is_recorded is False and "raised" in receipt.detail


def test_a_sink_bug_still_refuses_a_critical_action():
    auditor = _mk(_Sink(boom=RuntimeError("bug in the sink")))
    with pytest.raises(AuditUnavailable):
        run(auditor.record("user.suspended"))


def test_the_refusal_does_not_leak_which_action_was_attempted():
    # The message reaches a client, and the fact that a specific privileged action was ATTEMPTED is
    # itself information.
    auditor = _mk(_Sink(AuditReceipt(status=FAILED, detail="down")))
    with pytest.raises(AuditUnavailable) as exc:
        run(auditor.record("permission.granted", actor="sub_a", resource="user:u_7"))
    assert "permission.granted" not in str(exc.value)
    assert "u_7" not in str(exc.value)


def test_criticality_is_a_declared_set_not_a_guess():
    auditor = _mk(_Sink())
    assert auditor.is_critical("user.suspended") is True
    assert auditor.is_critical("profile.updated") is False
