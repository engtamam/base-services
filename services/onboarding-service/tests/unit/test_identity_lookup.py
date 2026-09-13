"""The identity-lookup seam that turns `once_per: verified_identity` into a real per-person key
by asking verification-service for a purpose-scoped token — never the raw claim_ref (onboarding-r2-03).

Covers the HTTP adapter's fail-CLOSED behaviour and the container wiring that only turns it on when
a verification source AND a claim type are both configured.
"""
from __future__ import annotations

import asyncio
import io
import json

from app.adapters.identity_lookup.http import HttpIdentityLookup
from app.adapters.identity_lookup.null import NullIdentityLookup


def run(coro):
    return asyncio.run(coro)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _adapter():
    return HttpIdentityLookup("http://verification:8000", "tok", "commercial_registration")


def test_http_returns_the_token_from_the_reply(monkeypatch):
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["hdr"] = req.headers
        return _Resp(json.dumps({"token": "PURPOSE-TOKEN"}).encode())

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    got = run(_adapter().claim_ref_for("t1", "s1"))
    assert got == "PURPOSE-TOKEN"
    assert "/internal/verification/identity-grant-token" in seen["url"]
    assert "tenant=t1" in seen["url"] and "claim_type=commercial_registration" in seen["url"]
    # constant internal-token header, never the user's identity
    assert seen["hdr"].get("X-internal-token") == "tok"


def test_http_fails_closed_when_unreachable(monkeypatch):
    def boom(req, timeout=None):
        raise OSError("connection refused")

    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert run(_adapter().claim_ref_for("t1", "s1")) == ""


def test_http_fails_closed_on_garbage_body(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp(b"not json"))
    assert run(_adapter().claim_ref_for("t1", "s1")) == ""


def test_http_empty_token_is_withheld(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(json.dumps({"token": ""}).encode()))
    assert run(_adapter().claim_ref_for("t1", "s1")) == ""


def test_null_lookup_always_withholds():
    assert run(NullIdentityLookup().claim_ref_for("t1", "s1")) == ""


# ── container wiring: on only when BOTH a verification source and a claim type are set ──────────
def _settings(**over):
    from app.config import Settings
    base = dict(INTERNAL_SERVICE_TOKEN="x" * 40, ENV="test")
    base.update(over)
    return Settings(**base)


def test_wiring_off_without_claim_type():
    from app.container import _build_identity_lookup
    s = _settings(ONBOARDING_SOURCES="verification=http://verification:8000")
    assert isinstance(_build_identity_lookup(s), NullIdentityLookup)


def test_wiring_off_without_source():
    from app.container import _build_identity_lookup
    s = _settings(ONBOARDING_IDENTITY_CLAIM_TYPE="commercial_registration")
    assert isinstance(_build_identity_lookup(s), NullIdentityLookup)


def test_wiring_on_with_both():
    from app.container import _build_identity_lookup
    s = _settings(ONBOARDING_SOURCES="verification=http://verification:8000",
                  ONBOARDING_IDENTITY_CLAIM_TYPE="commercial_registration")
    lk = _build_identity_lookup(s)
    assert isinstance(lk, HttpIdentityLookup) and lk.driver == "http"
