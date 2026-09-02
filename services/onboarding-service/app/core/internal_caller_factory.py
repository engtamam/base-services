"""Build the /internal caller-identity verifier + per-route policy from config, once, and cache them.

Kept OUT of the container on purpose: the container's shape varies service to service, but the internal
tier is uniform, so resolving these from settings here lets the seam propagate to every service by
touching only config.py and api/internal.py — never each service's bespoke container. Cached by the
settings VALUES (all hashable), so the verifier (and any JWKS clients it holds) is built once and
reused; a test that changes the env simply keys a fresh build.
"""
from __future__ import annotations

from functools import lru_cache

from app.adapters.internal_caller.shared import SharedTokenCaller
from app.core.internal_policy import InternalRoutePolicy
from app.ports.errors import ConfigError


@lru_cache(maxsize=32)
def _build_caller(driver: str, token: str, public_keys_json: str, jwks_json: str,
                  allow_fallback: bool):
    if driver == "shared":
        return SharedTokenCaller(token)
    from app.adapters.internal_caller.assertion import (AssertionCaller, JwksKeyResolver,
                                                        StaticKeyResolver)
    from app.config import _json_str_map
    if driver == "static":
        resolver = StaticKeyResolver(_json_str_map(public_keys_json))
    elif driver == "jwks":
        resolver = JwksKeyResolver(_json_str_map(jwks_json))
    else:
        raise ConfigError(
            f"unknown INTERNAL_CALLER_DRIVER: {driver!r} — allowed: shared, static, jwks")
    return AssertionCaller(resolver, allow_token_fallback=allow_fallback, expected_token=token)


@lru_cache(maxsize=32)
def _build_policy(policy_json: str) -> InternalRoutePolicy:
    from app.config import _json_any_map
    raw = _json_any_map(policy_json)
    return InternalRoutePolicy(
        {k: [str(x) for x in (v if isinstance(v, list) else [v])] for k, v in raw.items()})


def resolve_internal_caller(settings):
    return _build_caller(
        settings.INTERNAL_CALLER_DRIVER, settings.INTERNAL_SERVICE_TOKEN,
        settings.INTERNAL_CALLER_PUBLIC_KEYS_JSON, settings.INTERNAL_CALLER_JWKS_JSON,
        settings.INTERNAL_CALLER_ALLOW_TOKEN_FALLBACK)


def resolve_internal_policy(settings) -> InternalRoutePolicy:
    return _build_policy(settings.INTERNAL_ROUTE_POLICY_JSON)
