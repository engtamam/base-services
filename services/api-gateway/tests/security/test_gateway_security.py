"""Security: identity/internal-token spoof stripping, SSRF-safe URL build."""
from __future__ import annotations

import asyncio

from app.core.models import Route
from tests.unit.test_gateway_core import (
    FakeForwarder, FakeVerifier, _gw, make_request, run,
)


def test_client_supplied_identity_is_stripped():
    # auth=none -> no identity injected; a client X-User-Id must NOT pass through.
    r = Route(prefix="/echo", upstream="http://up", auth="none")
    fwd = FakeForwarder()
    gw = _gw(r, forwarder=fwd)
    run(gw.handle(make_request(headers={
        "X-User-Id": "attacker", "X-Internal-Token": "forged", "X-Tenant-Id": "evil",
    })))
    hdrs = fwd.last["headers"]
    assert hdrs.get("X-User-Id") is None            # client copy dropped
    assert hdrs["X-Internal-Token"] == "INTERNAL"   # replaced with the gateway's real token
    assert "X-Tenant-Id" not in hdrs


def test_verified_identity_overrides_client_spoof():
    r = Route(prefix="/echo", upstream="http://up", auth="required")
    fwd = FakeForwarder()
    gw = _gw(r, forwarder=fwd, verifier=FakeVerifier(claims={"sub": "real", "tenant": "t", "caps": []}))
    run(gw.handle(make_request(headers={"Authorization": "Bearer x", "X-User-Id": "attacker"})))
    assert fwd.last["headers"]["X-User-Id"] == "real"


def test_build_url_cannot_change_host():
    # A crafted path is only ever appended to the fixed upstream host — the URL
    # authority (host:port) can never be changed by the caller (SSRF-safe).
    from urllib.parse import urlsplit

    r = Route(prefix="/echo", upstream="http://safe-upstream:8001")
    for crafted in ("/echo/../../etc", "/echo/@evil.com/x", "/echo//evil.com", "/echo/x"):
        assert urlsplit(r.build_url(crafted)).netloc == "safe-upstream:8001"


def test_hop_by_hop_headers_dropped():
    r = Route(prefix="/echo", upstream="http://up")
    fwd = FakeForwarder()
    run(_gw(r, forwarder=fwd).handle(make_request(headers={"Connection": "keep-alive", "Host": "x"})))
    lowered = {k.lower() for k in fwd.last["headers"]}
    assert "connection" not in lowered and "host" not in lowered
