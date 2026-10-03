"""Security MEDIUM remediation: internal onboarding rate-limit (notification-017) and loud tenant-
resolution failures (notification-018)."""
from __future__ import annotations

import asyncio
import logging

import pytest


# ── notification-017: the internal onboarding twins carry the rate-limit dependency ──
def test_internal_onboarding_routes_are_rate_limited():
    from app.api.internal import router
    from app.api.ratelimit import rate_limit
    targets = {"/internal/whatsapp/embedded-signup", "/internal/whatsapp/register",
               "/internal/whatsapp/two-step-pin"}
    seen = {}
    for r in router.routes:
        path = getattr(r, "path", "")
        if path in targets:
            deps = [d.dependency for d in getattr(r, "dependencies", [])]
            seen[path] = rate_limit in deps
    assert targets <= set(seen), f"missing routes: {targets - set(seen)}"
    assert all(seen.values()), f"not rate-limited: {[p for p, ok in seen.items() if not ok]}"


# ── notification-018: a tenant-resolution FAILURE is logged, not silently a miss ──
def test_tenant_of_logs_on_failure(caplog):
    from app.api.webhooks import _tenant_of

    class _BoomIndex:
        async def tenant_of(self, phone_id):
            raise RuntimeError("index down")

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(_tenant_of(_BoomIndex(), "123"))
    assert out == ""                                      # still never stops the fan-out
    assert any("tenant_of_failed" in r.message for r in caplog.records)


def test_tenant_of_legitimate_miss_is_silent(caplog):
    from app.api.webhooks import _tenant_of

    class _EmptyIndex:
        async def tenant_of(self, phone_id):
            return None

    with caplog.at_level(logging.WARNING):
        out = asyncio.run(_tenant_of(_EmptyIndex(), "123"))
    assert out == ""
    assert not any("tenant_of_failed" in r.message for r in caplog.records)
