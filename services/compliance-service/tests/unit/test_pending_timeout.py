"""A target that says "I'll push my ack later" and never does must not hold a DSAR open for ever.

A 202 used to set next_at = 0.0 ("never due again") plus a short-circuit in _is_due. So a PENDING
target was not acked, not failed and not exhausted — therefore absent from `stuck` too. The DSAR
stayed open past its statutory deadline, and the only trace was `overdue` in a listing nothing
alerts on.

A push is not a guarantee. It can be lost to a crash, a deploy, or a dropped queue message, and
none of those announce themselves. The waiting has to be bounded.
"""
from __future__ import annotations

import asyncio

from app.config import Settings
from app.core.models import Dsar, new_id
from app.core.privacy_dispatcher import PrivacyDispatcher
from app.ports.privacy_transport import ACKED, FAILED, PENDING, EraseOutcome

_TOK = "x" * 40
_TIMEOUT = 3600.0


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = 1_000.0

    def now(self):
        return self.t


class _Dsars:
    async def save(self, d):
        return None


class _Transport:
    driver = "fake"

    def __init__(self, outcome):
        self.outcome = outcome
        self.calls = []

    async def erase(self, **kw):
        self.calls.append(kw)
        out = self.outcome
        return out.pop(0) if isinstance(out, list) and len(out) > 1 else (
            out[0] if isinstance(out, list) else out)


def _settings(**kw):
    base = dict(INTERNAL_SERVICE_TOKEN=_TOK,
                PRIVACY_TARGETS="billing-service=http://billing:8080",
                PRIVACY_MAX_ATTEMPTS=3, PRIVACY_BACKOFF_MS=1000, PRIVACY_CONCURRENCY=5,
                PRIVACY_PENDING_TIMEOUT_SECONDS=int(_TIMEOUT))
    base.update(kw)
    return Settings(**base)


def _dsar(*targets):
    return Dsar(id=new_id("dsar_"), tenant="acme", subject="u_7", type="erasure",
                targets={t: False for t in targets})


def _mk(transport, clock):
    return PrivacyDispatcher(dsars=_Dsars(), targets=None, transport=transport,
                             settings=_settings(), clock=clock, secrets=None, telemetry=None)


_KEY = "billing-service:invoices"


def test_a_pending_target_is_not_redialed_before_the_timeout():
    """The 202 still means something. Re-dialing every sweep would restart a large erasure
    repeatedly, which is the behaviour the original short-circuit existed to prevent."""
    clock = _Clock()
    t = _Transport(EraseOutcome(status=PENDING))
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    run(disp.drive(d))
    assert len(t.calls) == 1

    clock.t += _TIMEOUT / 2
    run(disp.drive(d))
    assert len(t.calls) == 1, "re-dialed a target that is legitimately still working"


def test_a_pending_target_IS_redialed_once_the_timeout_passes():
    """THE fix. Before this, no amount of elapsed time could make it due again."""
    clock = _Clock()
    t = _Transport(EraseOutcome(status=PENDING))
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    run(disp.drive(d))
    clock.t += _TIMEOUT + 1
    run(disp.drive(d))
    assert len(t.calls) == 2, "a lost push left the DSAR waiting for ever"


def test_a_target_that_never_pushes_eventually_becomes_STUCK():
    """Visible and actionable, rather than an open DSAR nobody can explain. It takes
    PRIVACY_MAX_ATTEMPTS of saying 'later' without delivering."""
    clock = _Clock()
    t = _Transport(EraseOutcome(status=PENDING))
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    summary = {}
    for _ in range(5):
        summary = run(disp.drive(d))
        clock.t += _TIMEOUT + 1

    assert summary["stuck"], "a target that always says 'later' never surfaced as stuck"
    assert summary["stuck"][0]["target"] == _KEY
    assert "never pushed" in summary["stuck"][0]["detail"]
    assert not d.is_complete()


def test_a_push_that_ARRIVES_still_completes_the_dsar():
    """The recovery path must not break the normal one."""
    clock = _Clock()
    t = _Transport(EraseOutcome(status=PENDING))
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    run(disp.drive(d))
    disp.record_pushed_ack(d, _KEY, {"erased": ["invoices"]})
    d.targets[_KEY] = True
    assert d.is_complete()


def test_a_redial_that_succeeds_acks_the_target():
    """A lost push is recovered by asking again — erase is idempotent by contract, so the second
    call is safe and the target answers properly this time."""
    clock = _Clock()
    t = _Transport([EraseOutcome(status=PENDING),
                    EraseOutcome(status=ACKED, ack={"erased": ["invoices"]})])
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    run(disp.drive(d))
    assert not d.is_complete()

    clock.t += _TIMEOUT + 1
    run(disp.drive(d))
    assert d.is_complete(), "the redial recovered nothing"


def test_awaiting_push_is_reported_while_it_is_still_recoverable():
    """Separate from `stuck` because it is not a failure — the target may be mid-erasure. But it
    is the state a lost push hides in, so it must be visible BEFORE the retries run out."""
    clock = _Clock()
    disp = _mk(_Transport(EraseOutcome(status=PENDING)), clock)
    d = _dsar(_KEY)

    summary = run(disp.drive(d))
    assert [a["target"] for a in summary["awaiting_push"]] == [_KEY]
    assert summary["stuck"] == []
    assert summary["awaiting_push"][0]["since"] == 1_000.0


def test_an_acked_target_is_not_reported_as_awaiting():
    clock = _Clock()
    disp = _mk(_Transport(EraseOutcome(status=ACKED, ack={"erased": []})), clock)
    summary = run(disp.drive(_dsar(_KEY)))
    assert summary["awaiting_push"] == []


def test_a_failed_target_still_backs_off_normally():
    """The PENDING change must not alter the FAILED path — different problem, different schedule."""
    clock = _Clock()
    t = _Transport(EraseOutcome(status=FAILED, detail="boom"))
    disp = _mk(t, clock)
    d = _dsar(_KEY)

    run(disp.drive(d))
    run(disp.drive(d))                    # immediately: still inside the backoff
    assert len(t.calls) == 1
