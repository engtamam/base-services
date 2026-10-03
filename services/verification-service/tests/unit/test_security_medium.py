"""Security MEDIUM remediation: provider egress allow-list + no-redirect (verification-010) and
user-safe status reason codes (verification-012)."""
from __future__ import annotations

import asyncio

from app.adapters.verifier.http import HttpVerifier
from app.core.models import ERROR, REFUSED, VERIFIED
from app.ports.verifier import Claim


def run(c):
    return asyncio.run(c)


# ── verification-010: a provider host outside EGRESS_ALLOWED_HOSTS is refused before any call ──
def test_provider_host_not_allowlisted_is_refused():
    v = HttpVerifier("reg", {"url": "https://evil.example/check"}, token="t",
                     allowed_hosts=frozenset({"registry.gov.sa"}))
    out = run(v.verify(Claim(claim_type="cr", value="123")))
    assert out.status == ERROR and "EGRESS_ALLOWED_HOSTS" in out.detail


def test_empty_allowlist_does_not_block():
    # unconfigured allow-list => unchanged behaviour (host check skipped); an unreachable host errors,
    # but NOT with the egress message
    v = HttpVerifier("reg", {"url": "https://127.0.0.1:9/check"}, token="t")
    out = run(v.verify(Claim(claim_type="cr", value="123")))
    assert "EGRESS_ALLOWED_HOSTS" not in out.detail


# ── verification-012: status() returns a mapped reason, never the raw provider detail ──
def test_status_reason_is_mapped_not_raw_detail():
    import time
    from app.core.verification_service import VerificationService

    class _Rec:
        verdict = REFUSED
        expires_at = 0.0
        evidence = {"detail": "RAW PROVIDER SECRET LEAK: internal-host unreachable at 10.0.0.5",
                    "checked_at": 1.0}
        def is_trusted(self, now):
            return False
        pending_until = 0.0

    class _Store:
        async def latest(self, *a):
            return _Rec()

    class _Clock:
        def now(self):
            return 100.0

    svc = VerificationService.__new__(VerificationService)
    svc._store = _Store()
    svc._clock = _Clock()
    out = run(svc.status("t1", "s1", "cr"))
    assert out["reason"] == "refused"                       # mapped code
    assert "RAW PROVIDER SECRET LEAK" not in str(out)       # raw detail never reaches the user
