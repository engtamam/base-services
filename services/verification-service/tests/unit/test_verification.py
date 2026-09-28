"""verification-service: every test here asks one question — can this say VERIFIED wrongly?

That is the only failure with no recovery. A wrongly refused customer complains and gets re-checked;
an outage retries. A wrongly verified identity is trust granted to whoever asked for it, and nothing
downstream can detect it: the welcome gift issues, the account opens, and the audit log faithfully
records that we verified them.

So the weight is on refusals and unknowns, not on the happy path.
"""
from __future__ import annotations

import asyncio
import io
import json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.verifier.http import HttpVerifier
from app.adapters.verifier.null import NullVerifier
from app.core.identity_ref import claim_ref, normalise
from app.core.models import ERROR, EXPIRED, PENDING, REFUSED, VERIFIED, Verification, new_id
from app.core.review import ReviewNotFound, ReviewRefused, ReviewService
from app.core.stores import IdentityRegistry, VerificationStore
from app.core.verification_service import UnsupportedClaim, VerificationService
from app.ports import verifier as vp

_SALT = "s" * 32
_NOW = 1_800_000_000.0


def run(c):
    return asyncio.run(c)


class _Clock:
    def __init__(self):
        self.t = _NOW

    def now(self):
        return self.t


class _Fake:
    driver = "fake"

    def __init__(self, status, boom=None):
        self.status, self.boom = status, boom

    async def verify(self, claim):
        if self.boom:
            raise self.boom
        return vp.Outcome(status=self.status, provider="fake", reference="R1")


def _svc(verifier=None, clock=None, ttl=None, store=None):
    st = store or MemoryDataStore()
    return VerificationService(
        store=VerificationStore(st), verifiers={"business": verifier} if verifier else {},
        clock=clock or _Clock(), pii_salt=_SALT, ttl_days=ttl or {}), st


# ── the null verifier: the most consequential null on the platform ────────────
def test_null_never_verifies():
    """A null sink loses an audit event; a null transport fails to erase. A null verifier that
    returned VERIFIED would let anyone claim any identity."""
    v = NullVerifier()
    out = run(v.verify(vp.Claim(claim_type="individual", value="1234567890")))
    assert out.status == vp.ERROR
    assert out.is_verified is False
    assert len(v.claims) == 1          # it records, so a test can prove it was REACHED


# ── unknowns must never become approvals ──────────────────────────────────────
@pytest.mark.parametrize("body", [
    {"data": {"status": "WEIRD"}},          # a value nobody configured
    {"data": {}},                            # verdict field missing
    {"other": 1},                            # path does not exist
])
def test_an_uninterpretable_response_is_error_not_verified(body):
    profile = {"url": "https://p/v", "verdict_path": "data.status", "verdict_ok": "OK"}
    with _urlopen(200, body):
        out = run(HttpVerifier("p", profile).verify(vp.Claim(claim_type="business", value="1")))
    assert out.status == vp.ERROR and not out.is_verified


def test_an_unreadable_body_is_error():
    profile = {"url": "https://p/v", "verdict_path": "data.status", "verdict_ok": "OK"}
    with _urlopen(200, raw=b"<html>maintenance</html>"):
        out = run(HttpVerifier("p", profile).verify(vp.Claim(claim_type="business", value="1")))
    assert out.status == vp.ERROR


def test_a_provider_error_is_not_a_refusal():
    """A 500 from a registry does not mean the company is unregistered. Treating it as refusal
    would lock out every legitimate customer during an outage."""
    profile = {"url": "https://p/v", "verdict_path": "data.status", "verdict_ok": "OK"}
    import urllib.error
    with _urlopen(exc=urllib.error.HTTPError("u", 500, "boom", {}, None)):
        out = run(HttpVerifier("p", profile).verify(vp.Claim(claim_type="business", value="1")))
    assert out.status == vp.ERROR and out.status != vp.REFUSED


def test_refused_and_error_are_distinguishable():
    """They look alike from a caller's chair and they are opposites: refused is an answer, error is
    the absence of one."""
    assert vp.Outcome(status=vp.REFUSED).is_answered is True
    assert vp.Outcome(status=vp.ERROR).is_answered is False
    assert vp.Outcome(status=vp.REFUSED).is_verified is False


def test_a_verifier_that_raises_cannot_produce_a_verified():
    svc, _ = _svc(_Fake(vp.VERIFIED, boom=RuntimeError("adapter bug")))
    rec = run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    assert rec.verdict == ERROR


def test_an_unconfigured_claim_type_is_refused_loudly():
    """Not silently verified, and not silently refused either — a platform that has not configured
    a verifier must say so rather than block every customer for an invisible reason."""
    svc, _ = _svc()
    with pytest.raises(UnsupportedClaim):
        run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))


# ── the raw identifier must never be stored ───────────────────────────────────
def test_the_raw_value_never_reaches_the_datastore():
    svc, st = _svc(_Fake(vp.VERIFIED))
    run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010123456"))
    dumped = json.dumps(st._data if hasattr(st, "_data") else {}, default=str)
    assert "1010123456" not in dumped


def test_claim_ref_never_leaves_the_service():
    """It is stable across tenants, so handing it out would let a caller correlate one person
    between them — the linkage the hashing exists to contain."""
    rec = Verification(id="v1", tenant="acme", subject="u1", claim_ref="clm_secret")
    assert "claim_ref" not in rec.public()
    assert "provider_state" not in rec.public()


def test_separators_and_case_do_not_create_a_second_identity():
    """An anti-abuse control a space defeats is not one."""
    a = claim_ref(_SALT, "business", "1010-123 456")
    b = claim_ref(_SALT, "business", "1010123456")
    assert a == b


def test_an_empty_salt_is_refused():
    """National id spaces are small and enumerable, so an unsalted digest is a lookup table."""
    with pytest.raises(ValueError, match="PII_SALT"):
        claim_ref("", "business", "1010123456")


def test_different_claim_types_do_not_collide():
    assert claim_ref(_SALT, "business", "123") != claim_ref(_SALT, "individual", "123")


# ── expiry: a stale verdict must stop being trusted ───────────────────────────
def test_an_expired_verdict_is_not_trusted():
    clock = _Clock()
    svc, st = _svc(_Fake(vp.VERIFIED), clock=clock, ttl={"business": 365})
    run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    assert run(svc.status("acme", "u1", "business"))["trusted"] is True
    clock.t += 400 * 86400
    after = run(svc.status("acme", "u1", "business"))
    assert after["trusted"] is False and after["verdict"] == EXPIRED


def test_expiry_is_evaluated_on_read_not_by_a_sweeper():
    """A sweeper that has not fired yet would leave a cancelled registration reading as verified,
    and that window is exactly when someone would use it."""
    clock = _Clock()
    svc, _ = _svc(_Fake(vp.VERIFIED), clock=clock, ttl={"business": 1})
    rec = run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    clock.t += 2 * 86400
    assert rec.is_trusted(clock.now()) is False     # no sweep ran


def test_stale_reports_as_expired_not_refused():
    """The caller must know whether to RE-ASK or to stop."""
    clock = _Clock()
    svc, _ = _svc(_Fake(vp.VERIFIED), clock=clock, ttl={"business": 1})
    run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    clock.t += 5 * 86400
    assert run(svc.status("acme", "u1", "business"))["verdict"] == EXPIRED


def test_a_verdict_with_no_ttl_never_expires():
    clock = _Clock()
    svc, _ = _svc(_Fake(vp.VERIFIED), clock=clock, ttl={})
    run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    clock.t += 100 * 365 * 86400
    assert run(svc.status("acme", "u1", "business"))["trusted"] is True


# ── async: a callback must not be able to rewrite history ─────────────────────
def test_a_replayed_callback_cannot_flip_a_settled_verdict():
    """Providers retry callbacks. A replay that reopened a decided verification could overwrite a
    refusal with an approval."""
    svc, _ = _svc(_Fake(vp.PENDING))
    rec = run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    run(svc.resolve(rec.id, verified=False))
    settled = run(svc.resolve(rec.id, verified=True))       # the replay, claiming success
    assert settled.verdict == REFUSED


def test_a_prompt_nobody_answers_lapses_rather_than_approving():
    clock = _Clock()
    svc, _ = _svc(_Fake(vp.PENDING), clock=clock)
    rec = run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))
    clock.t += 10_000                                       # past VERIFY_PENDING_TTL
    late = run(svc.resolve(rec.id, verified=True))
    assert late.verdict == EXPIRED                          # a late yes is not trusted


def test_status_of_a_never_checked_claim_is_not_trusted():
    svc, _ = _svc(_Fake(vp.VERIFIED))
    s = run(svc.status("acme", "nobody", "business"))
    assert s["trusted"] is False and s["verdict"] is None


# ── the anti-abuse registry ───────────────────────────────────────────────────
def test_the_registry_answers_first_seen_exactly_once():
    reg = IdentityRegistry(MemoryDataStore())
    ref = claim_ref(_SALT, "business", "1010123456")
    assert run(reg.remember(ref, _NOW))["first_seen"] is True
    assert run(reg.remember(ref, _NOW))["first_seen"] is False
    assert run(reg.seen(ref)) is True


# ── manual review ─────────────────────────────────────────────────────────────
def _pending(store, subject="u1"):
    v = Verification(id=new_id("ver_"), tenant="acme", subject=subject, claim_type="business",
                     verdict=PENDING, created_at=1.0)
    run(store.put(v))
    return v


def test_a_reviewer_cannot_decide_their_own_claim():
    store = VerificationStore(MemoryDataStore())
    v = _pending(store, subject="sub_rev")
    r = ReviewService(store=store, clock=_Clock())
    with pytest.raises(ReviewRefused, match="own claim"):
        run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_rev"))


def test_a_settled_verification_cannot_be_re_decided():
    """Re-deciding would rewrite a decision someone already relied on."""
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)
    r = ReviewService(store=store, clock=_Clock())
    run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_a"))
    with pytest.raises(ReviewRefused, match="already"):
        run(r.decide(v.id, tenant="acme", approve=False, reviewer="sub_b"))


def test_two_eyes_needs_two_different_reviewers():
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)
    r = ReviewService(store=store, clock=_Clock(), two_eyes=True)
    first = run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_a"))
    assert first.verdict == PENDING                 # one signature is not a decision
    same = run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_a"))
    assert same.verdict == PENDING                  # approving twice yourself is one pair of eyes
    second = run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_b"))
    assert second.verdict == VERIFIED


def test_a_reviewer_cannot_decide_another_tenants_claim():
    """Cross-tenant IDOR: a reviewer holding verification.review in tenant B must not decide a
    record that belongs to tenant 'acme' by id — it reads as a 404, no existence leak."""
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)                                  # tenant 'acme'
    r = ReviewService(store=store, clock=_Clock())
    with pytest.raises(ReviewNotFound):
        run(r.decide(v.id, tenant="other_shop", approve=True, reviewer="rev_b"))
    # and the record is untouched — still pending in its own tenant
    assert run(store.get(v.id)).verdict == PENDING


def test_a_review_records_who_decided():
    store = VerificationStore(MemoryDataStore())
    v = _pending(store)
    r = ReviewService(store=store, clock=_Clock())
    out = run(r.decide(v.id, tenant="acme", approve=True, reviewer="sub_rev1", note="checked CR certificate"))
    assert out.evidence["reviewer"] == "sub_rev1"
    assert out.evidence["provider"] == "manual"


# ── helper ────────────────────────────────────────────────────────────────────
class _Resp(io.BytesIO):
    def __init__(self, status, body=b""):
        super().__init__(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(status=200, body=None, raw=None, exc=None):
    from unittest.mock import patch
    payload = raw if raw is not None else json.dumps(body or {}).encode()

    def fake(req, timeout=None):
        if exc:
            raise exc
        return _Resp(status, payload)

    return patch("app.adapters.verifier.http.urllib.request.urlopen", fake)


# ── verification-001: the VERIFIED grant is audited BEFORE it is persisted ──────
class _FailAuditor:
    """A critical audit sink that is DOWN for the trust-granting action only."""
    def __init__(self):
        self.calls = []

    async def record(self, action, **kw):
        from app.core.audit import AuditUnavailable
        self.calls.append(action)
        if action == "identity.verified":
            raise AuditUnavailable("audit log unreachable")


class _OkAuditor:
    def __init__(self):
        self.calls = []

    async def record(self, action, **kw):
        self.calls.append(action)


def test_verified_grant_is_not_committed_when_the_critical_audit_is_down():
    # If the identity.verified audit fails, NOTHING may be stored: a 503 must mean the grant did not
    # happen, not that it happened unprovably. Before the fix put() ran first and status() returned
    # VERIFIED while the caller saw a 503 (verification-001).
    from app.core.audit import AuditUnavailable
    st = MemoryDataStore()
    aud = _FailAuditor()
    svc = VerificationService(store=VerificationStore(st), verifiers={"business": _Fake(vp.VERIFIED)},
                              clock=_Clock(), pii_salt=_SALT, auditor=aud)
    with pytest.raises(AuditUnavailable):
        run(svc.verify(tenant="t", subject="s", claim_type="business", value="X"))
    # write-ahead: the audit was attempted, and no VERIFIED record was persisted.
    assert aud.calls == ["identity.verified"]
    assert run(VerificationStore(st).latest("t", "s", "business")) is None
    assert run(svc.status("t", "s", "business"))["trusted"] is False


def test_verified_grant_commits_and_audits_when_the_log_is_up():
    st = MemoryDataStore()
    aud = _OkAuditor()
    svc = VerificationService(store=VerificationStore(st), verifiers={"business": _Fake(vp.VERIFIED)},
                              clock=_Clock(), pii_salt=_SALT, auditor=aud)
    rec = run(svc.verify(tenant="t", subject="s", claim_type="business", value="X"))
    assert rec.verdict == VERIFIED
    assert aud.calls == ["identity.verified"]
    assert run(VerificationStore(st).latest("t", "s", "business")).verdict == VERIFIED


def test_a_refusal_is_not_blocked_by_a_down_audit_log():
    # Only VERIFIED is fail-closed. A refusal must still be recorded (as REFUSED) even if the audit
    # sink is unhappy — refusing to refuse someone because the log is down helps nobody.
    st = MemoryDataStore()
    aud = _FailAuditor()   # only raises on identity.verified; identity.refused is non-critical
    svc = VerificationService(store=VerificationStore(st), verifiers={"business": _Fake(vp.REFUSED)},
                              clock=_Clock(), pii_salt=_SALT, auditor=aud)
    rec = run(svc.verify(tenant="t", subject="s", claim_type="business", value="X"))
    assert rec.verdict == REFUSED
    assert run(VerificationStore(st).latest("t", "s", "business")).verdict == REFUSED


# ── verification HIGH regressions (003/005/006/007/008) ─────────────────────────
class _YieldStore(MemoryDataStore):
    """Yields on get() so two concurrent resolves interleave at the read — exposes a check-then-write
    race deterministically without threads."""
    async def get(self, collection, key):
        import asyncio as _a
        await _a.sleep(0)
        return await super().get(collection, key)


def test_concurrent_resolve_settles_once_and_does_not_double_settle():
    # verification-003: two retried 'verified' callbacks for one PENDING id must not both settle.
    import asyncio as _a
    st = _YieldStore()
    svc = VerificationService(store=VerificationStore(st), verifiers={"business": _Fake(vp.PENDING)},
                              clock=_Clock(), pii_salt=_SALT)
    rec = run(svc.verify(tenant="acme", subject="u1", claim_type="business", value="1010"))

    async def _race():
        return await _a.gather(svc.resolve(rec.id, verified=True),
                               svc.resolve(rec.id, verified=True))
    a, b = run(_race())
    assert a.verdict == VERIFIED and b.verdict == VERIFIED       # both observe the SAME settled state
    final = run(VerificationStore(st).get(rec.id))
    assert final.verdict == VERIFIED


def test_status_lapses_a_pending_prompt_on_read():
    # verification-005: a PENDING record past its pending_until reports EXPIRED on read, even if no
    # sweep has run and no callback arrived.
    st = MemoryDataStore()
    store = VerificationStore(st)
    rec = Verification(id=new_id("ver_"), tenant="acme", subject="u1", claim_type="business",
                       claim_ref="r", verdict=PENDING, created_at=1.0, pending_until=100.0)
    run(store.put(rec))
    svc = VerificationService(store=store, verifiers={}, clock=_Clock(), pii_salt=_SALT)
    svc._clock.t = 200.0                                          # past pending_until
    s = run(svc.status("acme", "u1", "business"))
    assert s["verdict"] == EXPIRED and s["trusted"] is False


def test_resolve_requires_the_provider_correlation_token():
    # verification-007: a pending prompt that captured a correlation reference refuses a callback that
    # cannot present it, and accepts the one that can.
    st = MemoryDataStore()
    store = VerificationStore(st)
    rec = Verification(id=new_id("ver_"), tenant="acme", subject="u1", claim_type="business",
                       claim_ref="r", verdict=PENDING, created_at=1.0, pending_until=_NOW + 10_000.0,
                       provider_state={"reference": "CORR-XYZ"},
                       evidence={"provider": "nafath"})
    run(store.put(rec))
    svc = VerificationService(store=store, verifiers={}, clock=_Clock(), pii_salt=_SALT)
    with pytest.raises(VerificationService.CorrelationMismatch):
        run(svc.resolve(rec.id, verified=True, reference="WRONG"))
    assert run(store.get(rec.id)).verdict == PENDING             # not settled by the bad callback
    ok = run(svc.resolve(rec.id, verified=True, reference="CORR-XYZ"))
    assert ok.verdict == VERIFIED


def test_erasure_severs_the_link_without_clobbering_a_concurrent_verdict():
    # verification-008: erase() re-applies subject='' onto the freshest row via cas, so a verdict a
    # reviewer/provider committed concurrently is preserved while the link is still severed.
    st = MemoryDataStore()
    store = VerificationStore(st)
    rec = Verification(id=new_id("ver_"), tenant="acme", subject="u1", claim_type="business",
                       claim_ref="r", verdict=VERIFIED, created_at=1.0)
    run(store.put(rec))
    from app.core.erasure import VerificationErasure
    res = run(VerificationErasure(store=store).erase("acme", "u1"))
    after = run(store.get(rec.id))
    assert after.subject == "" and after.verdict == VERIFIED     # link gone, decision kept
    assert any("verification_subject_links" in e for e in res["erased"])
