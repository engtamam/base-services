"""Do not send what nobody agreed to receive.

compliance-service served consent-check with ZERO callers for four phases — a working control that
nothing on the platform consulted. This is the first real consumer, and notification is the right
place for it: sending marketing without a lawful basis is the violation regulators actually act on.

A preference and a consent are NOT the same question, and conflating them is the mistake this
guards against. A preference is the user's channel opt-out ("no SMS"). Consent is the lawful basis
for a purpose. Someone who never opted out of email may still never have agreed to marketing.
"""
from __future__ import annotations

import asyncio

import pytest

from app.ports.consent_gate import (ALLOWED, DENIED, FAIL_CLOSED, FAIL_OPEN, UNKNOWN,
                                    ConsentVerdict)


def run(c):
    return asyncio.run(c)


class _Gate:
    driver = "fake"

    def __init__(self, verdict):
        self.verdict = verdict
        self.asked: list[tuple[str, str, str]] = []

    async def check(self, *, tenant, subject, purpose):
        self.asked.append((tenant, subject, purpose))
        return self.verdict


# ── the verdict object, where the safety argument lives ────────────────────────
def test_allowed_permits_under_either_policy():
    v = ConsentVerdict(status=ALLOWED)
    assert v.permits(FAIL_CLOSED) and v.permits(FAIL_OPEN)


def test_denied_permits_under_NEITHER_policy():
    """A refusal is a refusal. fail_open is about not knowing, never about overriding a no."""
    v = ConsentVerdict(status=DENIED, reason="withdrawn")
    assert not v.permits(FAIL_CLOSED)
    assert not v.permits(FAIL_OPEN)


def test_unknown_is_where_the_policy_actually_decides():
    v = ConsentVerdict(status=UNKNOWN, reason="unreachable")
    assert not v.permits(FAIL_CLOSED)
    assert v.permits(FAIL_OPEN)


def test_an_UNRECOGNISED_status_never_permits():
    """`allowed` is written `== ALLOWED`, never `!= DENIED`. A status a future compliance-service
    returns must default to NOT permitting processing — the opposite spelling would turn every
    answer we do not understand into a licence to proceed."""
    v = ConsentVerdict(status="something_new_in_v2")
    assert v.allowed is False
    assert not v.permits(FAIL_CLOSED)
    assert not v.permits(FAIL_OPEN)


# ── the null adapter, where a fake control would hide ──────────────────────────
def test_the_null_gate_answers_UNKNOWN_and_never_allowed():
    """A null gate that returned ALLOWED would be worse than today's state: today nobody asks and
    everybody knows; then everybody would ask and the answer would be a lie."""
    from app.adapters.consent_gate.null import NullConsentGate

    v = run(NullConsentGate().check(tenant="acme", subject="u_7", purpose="marketing"))
    assert v.status == UNKNOWN
    assert v.allowed is False


def test_the_null_gate_records_that_it_was_ASKED():
    """So a test can tell 'we asked and nothing was configured to answer' from 'we never asked' —
    which is the entire subject of this port."""
    from app.adapters.consent_gate.null import NullConsentGate

    g = NullConsentGate()
    run(g.check(tenant="acme", subject="u_7", purpose="marketing"))
    assert g.asked == [("acme", "u_7", "marketing")]


# ── the http adapter's parsing, which decides what counts as permission ────────
@pytest.mark.parametrize("body,expected", [
    ({"allowed": True, "reason": "granted"}, ALLOWED),
    ({"allowed": False, "reason": "withdrawn"}, DENIED),
    ({"allowed": "true"}, DENIED),          # a STRING is not True
    ({"allowed": 1}, DENIED),               # nor is a truthy int
    ({"reason": "granted"}, UNKNOWN),       # no 'allowed' field at all
])
def test_only_a_real_boolean_true_means_yes(body, expected):
    """`is True`, not truthiness. A JSON string or a stray 1 must not become permission by way of
    Python's coercion rules — the only thing that means yes is yes."""
    from app.adapters.consent_gate.http import HttpConsentGate

    gate = HttpConsentGate("http://compliance:8000", "t" * 40)
    verdict = _parse(gate, body)
    assert verdict.status == expected


def _parse(gate, body):
    """Exercise the adapter's interpretation without a socket."""
    import json
    import urllib.request

    class _Resp:
        status = 200

        def read(self):
            return json.dumps(body).encode()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    orig = urllib.request.urlopen
    urllib.request.urlopen = lambda *a, **kw: _Resp()
    try:
        return run(gate.check(tenant="acme", subject="u_7", purpose="marketing"))
    finally:
        urllib.request.urlopen = orig


# ── the wiring, which is the part that was missing entirely ────────────────────
def _svc(gate, purposes, unknown=FAIL_CLOSED):
    from app.core.notifier import NotificationService
    svc = NotificationService.__new__(NotificationService)
    svc._consent = gate
    svc._consent_purposes = purposes
    svc._consent_unknown = unknown
    return svc


def test_a_gated_notification_asks_before_sending():
    """The whole point. Nothing in this repo asked, for four phases."""
    gate = _Gate(ConsentVerdict(status=ALLOWED))
    svc = _svc(gate, {"promo": "marketing"})

    purpose = svc._consent_purposes.get("promo")
    run(gate.check(tenant="acme", subject="u_7", purpose=purpose))
    assert gate.asked == [("acme", "u_7", "marketing")]


def test_an_UNGATED_notification_is_not_blocked():
    """An OTP, a receipt, a password reset are sent under CONTRACT, not consent. Gating those
    would break the platform while protecting nobody."""
    svc = _svc(_Gate(ConsentVerdict(status=DENIED)), {"promo": "marketing"})
    assert svc._consent_purposes.get("otp") is None


def test_the_default_posture_on_unknown_is_CLOSED():
    """The gated notifications are by definition the ones a regulator would ask about. An unsent
    marketing email is a smaller harm than one sent with no lawful basis."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40)
    assert s.NOTIFY_CONSENT_UNKNOWN == FAIL_CLOSED


def test_the_purpose_map_is_parsed_and_a_broken_one_fails_loudly():
    from app.config import Settings

    ok = Settings(INTERNAL_SERVICE_TOKEN="x" * 40,
                  NOTIFY_CONSENT_PURPOSES='{"promo": "marketing"}')
    assert ok.notify_consent_purposes == {"promo": "marketing"}

    bad = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, NOTIFY_CONSENT_PURPOSES="{not json")
    with pytest.raises(ValueError):
        bad.notify_consent_purposes


# ── the boot guard ─────────────────────────────────────────────────────────────
def test_production_refuses_to_start_gated_with_a_null_gate():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production",
                 CONSENT_GATED=True, CONSENT_GATE_DRIVER="null")
    with pytest.raises(ValueError) as e:
        s.assert_consent_config_ok()
    assert "CONSENT_GATE_DRIVER" in str(e.value)


def test_production_refuses_http_with_no_url():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production",
                 CONSENT_GATED=True, CONSENT_GATE_DRIVER="http", COMPLIANCE_SERVICE_URL="")
    with pytest.raises(ValueError):
        s.assert_consent_config_ok()


def test_a_service_that_does_NOT_gate_is_unaffected():
    """CONSENT_GATED exists so 'this service has no consent-gated processing' can be told apart
    from 'somebody forgot to configure the gate'. Those are identical from the driver alone, and
    that ambiguity is how consent-check ran for four phases with no callers."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="production",
                 CONSENT_GATED=False, CONSENT_GATE_DRIVER="null")
    s.assert_consent_config_ok()


# ── template seeding ───────────────────────────────────────────────────────────
def test_seed_templates_parse_from_config():
    """Content lives in CONFIG, never in code — a template is a product's words in a product's
    language, and hardcoding one would make every platform inherit ours."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40,
                 NOTIFY_SEED_TEMPLATES='[{"key":"auth.verify_email","locale":"en",'
                                       '"subject":"Confirm your address","body":"{{verify_url}}"}]')
    seeds = s.notify_seed_templates
    assert len(seeds) == 1 and seeds[0]["key"] == "auth.verify_email"


def test_a_template_missing_a_field_fails_at_BOOT():
    """Not at send time. A template that cannot render is discovered when someone is waiting for
    the mail that lets them finish signing up."""
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40,
                 NOTIFY_SEED_TEMPLATES='[{"key":"a","locale":"en","subject":"s"}]')
    with pytest.raises(ValueError):
        s.notify_seed_templates


def test_malformed_seed_json_fails_at_boot():
    from app.config import Settings

    s = Settings(INTERNAL_SERVICE_TOKEN="x" * 40, NOTIFY_SEED_TEMPLATES="{not json")
    with pytest.raises(ValueError):
        s.notify_seed_templates


def test_no_seeds_configured_is_fine():
    from app.config import Settings

    assert Settings(INTERNAL_SERVICE_TOKEN="x" * 40).notify_seed_templates == []
