"""Security MEDIUM remediation: tenant claim drift (api-gateway-004), fail-open config parse
(api-gateway-007), and /metrics tracking the internal-caller policy (api-gateway-013)."""
from __future__ import annotations

import pytest

from app.core.gateway import Gateway


class _F:
    driver = "fake"

    async def forward(self, *a, **k):
        return (200, {}, b"")

    async def close(self):
        pass


def _gw():
    return Gateway(route_table=_F(), forwarder=_F(), token_verifier=_F(), rate_limiter=_F(),
                   cache=_F(), internal_token="INTERNAL", default_cache_ttl=0)


# ── api-gateway-004: the forwarded identity derives tenant from `tid` too ────────
def test_identity_from_resolves_tid_claim():
    ident = _gw()._identity_from({"sub": "u1", "tid": "acme", "caps": ["x"]})
    assert ident["tenant"] == "acme"       # used to drop tid and forward an empty tenant


def test_identity_from_prefers_tenant_over_tid():
    ident = _gw()._identity_from({"sub": "u1", "tenant": "t1", "tid": "acme"})
    assert ident["tenant"] == "t1"


# ── api-gateway-007: malformed security config fails closed, not silently empty ──
def test_json_map_raises_on_malformed_json():
    from app.config import _json_any_map
    with pytest.raises(ValueError):
        _json_any_map('{"a": ')          # a typo must not silently disable an access-control map


def test_json_map_empty_is_fine():
    from app.config import _json_any_map
    assert _json_any_map("") == {}
    assert _json_any_map("   ") == {}


def test_json_map_rejects_non_object():
    from app.config import _json_any_map
    with pytest.raises(ValueError):
        _json_any_map("[1,2,3]")


# ── api-gateway-013: /metrics uses the shared internal-caller dependency ─────────
def test_metrics_uses_the_canonical_internal_dependency():
    import app.api.metrics as m
    from app.api.internal import require_internal_token
    # The route's guard IS the fleet dependency (identity + policy), not a bespoke flat-token compare.
    deps = [d.dependency for d in m.router.routes[0].dependencies]
    assert require_internal_token in deps
