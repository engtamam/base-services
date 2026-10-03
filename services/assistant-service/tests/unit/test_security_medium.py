"""Security MEDIUM remediation: redaction gaps (assistant-014), SSRF port/cleartext (assistant-011),
and credential single-flight (assistant-012)."""
from __future__ import annotations

import asyncio

import pytest


# ── assistant-014: Amex + JWT are redacted ──────────────────────────────────────
def test_amex_is_redacted():
    from app.core.redaction import redact
    r = redact("my card is 3782 822463 10005 ok")
    assert "3782" not in r.text and "[card]" in r.text


def test_jwt_is_redacted():
    from app.core.redaction import redact
    jwt = "eyJhbGciOiJIUzI1NiJ9." + "a" * 40 + "." + "b" * 43
    r = redact(f"token {jwt} end")
    assert jwt not in r.text and "[secret]" in r.text


# ── assistant-011: SSRF port + cleartext-credential guards ───────────────────────
def test_non_standard_port_is_blocked():
    from app.core import ssrf
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("https://example.com:6379/x", allow_private=False)


def test_non_standard_port_allowed_when_listed():
    from app.core import ssrf
    # resolves a public host; we only assert it does not raise on the PORT check
    try:
        ssrf.validate("https://example.com:8443/x", allowed_ports=frozenset({8443}))
    except ssrf.SsrfBlocked as e:
        assert "port" not in str(e).lower()


def test_credentials_never_over_http_even_when_https_relaxed():
    from app.core import ssrf
    with pytest.raises(ssrf.SsrfBlocked):
        ssrf.validate("http://example.com/x", require_https=False, carries_credentials=True)


# ── assistant-012: concurrent credential refresh is single-flight ────────────────
def test_model_credentials_single_flight():
    from app.core.model_credentials import ModelCredentials

    class _Secrets:
        def __init__(self):
            self.calls = 0

        async def get(self, *, tenant, name, fallback):
            self.calls += 1
            await asyncio.sleep(0.01)
            return "k"

    class _S:
        MODEL_API_KEY = ""

    secrets = _Secrets()
    mc = ModelCredentials(_S(), secrets, name="MODEL_API_KEY", ttl_s=60)

    async def _go():
        await asyncio.gather(*[mc.get("t") for _ in range(8)])

    asyncio.run(_go())
    assert secrets.calls == 1          # eight concurrent misses, one resolve
