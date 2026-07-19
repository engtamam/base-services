"""PrivacyDispatcher: the fan-out that makes "erased" true rather than merely recorded.

Every test here is a way of asking whether a DSAR can reach `completed` while a target still holds
the subject's data. If any of these go green wrongly, the service lies to a regulator.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.core.models import Dsar, new_id
from app.core.privacy_dispatcher import PrivacyDispatcher
from app.ports.privacy_transport import ACKED, FAILED, PENDING, EraseOutcome

_TOK = "x" * 40


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = 1_000.0

    def now(self):
        return self.t


class _Dsars:
    def __init__(self):
        self.saves = 0

    async def save(self, d):
        self.saves += 1


class _Transport:
    """Scripted per-target outcomes; records every call so we can prove what was dialed."""
    driver = "fake"

    def __init__(self, script):
        self.script = script          # url-substring -> outcome | list of outcomes
        self.calls = []

    async def erase(self, **kw):
        self.calls.append(kw)
        for frag, out in self.script.items():
            if frag in kw["url"]:
                if isinstance(out, list):
                    return out.pop(0) if len(out) > 1 else out[0]
                return out
        return EraseOutcome(status=FAILED, detail="unscripted")


def _settings(**kw):
    base = dict(INTERNAL_SERVICE_TOKEN=_TOK,
                PRIVACY_TARGETS="identity-service=http://identity:8080,"
                                "billing-service=http://billing:8080",
                PRIVACY_MAX_ATTEMPTS=3, PRIVACY_BACKOFF_MS=1000, PRIVACY_CONCURRENCY=5)
    base.update(kw)
    return Settings(**base)


def _dsar(*targets):
    return Dsar(id=new_id("dsar_"), tenant="acme", subject="u_7", type="erasure",
                targets={t: False for t in targets})


def _mk(transport, settings=None, clock=None, secrets=None):
    return PrivacyDispatcher(dsars=_Dsars(), targets=None, transport=transport,
                             settings=settings or _settings(), clock=clock or _Clock(),
                             secrets=secrets, random=lambda: 1.0)


_OK = EraseOutcome(status=ACKED, ack={"erased": ["profile"], "retained": []})


def test_all_targets_ack_completes_the_dsar():
    d = _dsar("identity-service:identity", "billing-service:invoices")
    out = run(_mk(_Transport({"identity": _OK, "billing": _OK})).drive(d))
    assert d.is_complete() and out["complete"] and out["remaining"] == []
    assert d.target_state["identity-service:identity"]["ack"] == _OK.ack   # evidence retained


def test_one_failing_target_keeps_the_dsar_incomplete():
    # The whole point: 1 of 2 erased is NOT "done".
    d = _dsar("identity-service:identity", "billing-service:invoices")
    t = _Transport({"identity": _OK, "billing": EraseOutcome(status=FAILED, detail="HTTP 500")})
    out = run(_mk(t).drive(d))
    assert not d.is_complete()
    assert out["remaining"] == ["billing-service:invoices"]


def test_a_202_target_is_pending_not_acked_and_is_not_redialed():
    d = _dsar("billing-service:invoices")
    t = _Transport({"billing": EraseOutcome(status=PENDING)})
    disp = _mk(t)
    run(disp.drive(d))
    assert not d.is_complete()
    run(disp.drive(d))                       # a second sweep must not dial it again
    assert len(t.calls) == 1
    assert d.target_state["billing-service:invoices"]["status"] == PENDING


def test_pushed_ack_completes_a_pending_target():
    d = _dsar("billing-service:invoices")
    disp = _mk(_Transport({"billing": EraseOutcome(status=PENDING)}))
    run(disp.drive(d))
    disp.record_pushed_ack(d, "billing-service:invoices", {"erased": ["invoices"]})
    d.targets["billing-service:invoices"] = True     # ack map is what completes it
    assert d.is_complete()


def test_acked_targets_are_never_redialed():
    d = _dsar("identity-service:identity", "billing-service:invoices")
    t = _Transport({"identity": _OK, "billing": EraseOutcome(status=FAILED, detail="boom")})
    clock, disp = _Clock(), None
    disp = PrivacyDispatcher(dsars=_Dsars(), targets=None, transport=t, settings=_settings(),
                             clock=clock, random=lambda: 1.0)
    run(disp.drive(d))
    clock.t += 10_000
    run(disp.drive(d))
    dialed = [c["url"] for c in t.calls]
    assert sum("identity" in u for u in dialed) == 1     # erased once, not twice
    assert sum("billing" in u for u in dialed) == 2      # retried


def test_backoff_defers_the_next_attempt():
    clock = _Clock()
    d = _dsar("billing-service:invoices")
    t = _Transport({"billing": EraseOutcome(status=FAILED, detail="boom")})
    disp = _mk(t, clock=clock)
    run(disp.drive(d))
    run(disp.drive(d))                     # immediately again: not due yet
    assert len(t.calls) == 1
    clock.t += 3600
    run(disp.drive(d))
    assert len(t.calls) == 2


def test_exhausted_target_stops_retrying_but_stays_visibly_stuck():
    # The failure mode this prevents: a DSAR quietly marked complete with data still out there.
    clock = _Clock()
    d = _dsar("billing-service:invoices")
    t = _Transport({"billing": EraseOutcome(status=FAILED, detail="HTTP 500")})
    disp = _mk(t, clock=clock)
    for _ in range(10):
        clock.t += 100_000
        out = run(disp.drive(d))
    assert len(t.calls) == 3                       # PRIVACY_MAX_ATTEMPTS
    assert not d.is_complete()
    assert out["remaining"] == ["billing-service:invoices"]
    assert out["stuck"] == [{"target": "billing-service:invoices", "attempts": 3,
                             "detail": "HTTP 500"}]


def test_target_in_the_data_map_but_absent_from_config_fails_loudly():
    # It holds her data (it is in the map). Not knowing its URL must never look like success.
    d = _dsar("acme-crm:crm_contacts")
    t = _Transport({})
    out = run(_mk(t).drive(d))
    assert t.calls == []                            # nothing dialed
    assert not d.is_complete() and out["remaining"] == ["acme-crm:crm_contacts"]
    assert "do not know where it lives" in d.target_state["acme-crm:crm_contacts"]["detail"]


def test_url_is_base_plus_the_targets_own_path():
    d = _dsar("identity-service:identity", "acme-crm:contacts")
    s = _settings(PRIVACY_TARGETS="identity-service=http://identity:8080,acme-crm=https://crm.acme",
                  PRIVACY_ERASE_PATHS="acme-crm=/api/gdpr/forget")
    t = _Transport({"identity": _OK, "crm.acme": _OK})
    run(_mk(t, settings=s).drive(d))
    urls = sorted(c["url"] for c in t.calls)
    assert urls == ["http://identity:8080/internal/privacy/erase",
                    "https://crm.acme/api/gdpr/forget"]


def test_third_party_gets_its_own_credential_and_never_our_internal_token():
    class _Secrets:
        def get(self, key):
            return "crm-secret" if key == "ACME_CRM_TOKEN" else None

    d = _dsar("identity-service:identity", "acme-crm:contacts")
    s = _settings(PRIVACY_TARGETS="identity-service=http://identity:8080,acme-crm=https://crm.acme",
                  PRIVACY_TARGET_SECRETS="acme-crm=ACME_CRM_TOKEN")
    t = _Transport({"identity": _OK, "crm.acme": _OK})
    run(_mk(t, settings=s, secrets=_Secrets()).drive(d))
    by_host = {c["url"]: c["token"] for c in t.calls}
    assert by_host["https://crm.acme/internal/privacy/erase"] == "crm-secret"
    assert by_host["http://identity:8080/internal/privacy/erase"] == _TOK
    assert _TOK not in by_host["https://crm.acme/internal/privacy/erase"]


def test_the_request_carries_tenant_subject_action_and_id():
    d = _dsar("identity-service:identity")
    t = _Transport({"identity": _OK})
    run(_mk(t).drive(d))
    call = t.calls[0]
    assert (call["tenant"], call["subject"], call["action"]) == ("acme", "u_7", "erasure")
    assert call["request_id"] == d.id
    assert call["timeout"] == 30


def test_concurrency_is_bounded_by_config():
    live = {"now": 0, "max": 0}

    class _Slow:
        driver = "slow"

        async def erase(self, **kw):
            live["now"] += 1
            live["max"] = max(live["max"], live["now"])
            await asyncio.sleep(0)
            live["now"] -= 1
            return _OK

    d = _dsar(*[f"svc{i}:cat" for i in range(10)])
    s = _settings(PRIVACY_TARGETS=",".join(f"svc{i}=http://s{i}:8080" for i in range(10)),
                  PRIVACY_CONCURRENCY=2)
    run(_mk(_Slow(), settings=s).drive(d))
    assert live["max"] <= 2
