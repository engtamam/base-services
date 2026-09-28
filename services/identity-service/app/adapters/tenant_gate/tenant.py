"""TenantServiceGate — a tenant's lifecycle status from tenant-service, cached at the mint.

    GET {TENANT_SERVICE_URL}/internal/tenants/{id}  ->  {"status": "active|suspended|deleted", ...}

This sits on the mint path (login, refresh, tenant select), so answers are cached per tenant for
TTL_SECONDS — short, because that is also the enforcement lag: suspend a tenant and its next mint is
refused within the TTL. A DELETED tenant answers 200 with status "deleted" (tenant-service tombstones,
it does not 404), so suspend/delete is a definite negative, not a lookup miss; a genuine 404 is
"unknown". Any other failure raises TenantGateUnavailable. The URL is config, never from a request.
"""
from __future__ import annotations

import time

from app.adapters.lazy import load
from app.core.internal_client import assertion_headers
from app.ports.errors import ConfigError
from app.ports.tenant_gate import DELETED, SUSPENDED, UNKNOWN, TenantGateUnavailable

_ALLOWED = {"active", SUSPENDED, DELETED}


class TenantServiceGate:
    driver = "tenant"

    def __init__(self, base_url: str, internal_token: str, *, settings,
                 timeout_ms: int, ttl_seconds: int, token_header: str = "X-Internal-Token") -> None:
        if not base_url:
            raise ConfigError(
                "IDENTITY_TENANT_STATUS_ENFORCEMENT=block requires TENANT_SERVICE_URL — a tenant "
                "authority that is not configured cannot be consulted")
        if not internal_token:
            raise ConfigError(
                "IDENTITY_TENANT_STATUS_ENFORCEMENT=block requires INTERNAL_SERVICE_TOKEN to call "
                "/internal/tenants/{id}")
        self._httpx = load("httpx", "tenantgate")
        self._url = base_url.rstrip("/") + "/internal/tenants/"
        self._token = internal_token
        self._settings = settings
        self._timeout = timeout_ms / 1000
        self._ttl = ttl_seconds
        self._token_header = token_header
        self._cache: dict[str, tuple[float, str]] = {}

    async def status(self, tenant: str) -> str:
        now = time.monotonic()
        hit = self._cache.get(tenant)
        if hit and hit[0] > now:
            return hit[1]

        headers = {self._token_header: self._token}
        try:
            headers.update(assertion_headers(self._settings, target="tenant-service"))
        except Exception:
            pass
        try:
            async with self._httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(self._url + tenant, headers=headers)
        except Exception as exc:
            raise TenantGateUnavailable(f"tenant-service unreachable: {exc.__class__.__name__}") from exc

        if r.status_code == 404:
            self._cache[tenant] = (now + self._ttl, UNKNOWN)
            return UNKNOWN
        if r.status_code != 200:
            raise TenantGateUnavailable(f"tenant-service answered {r.status_code}")
        try:
            st = str(r.json().get("status") or "").lower()
        except Exception as exc:
            raise TenantGateUnavailable("tenant-service returned a malformed body") from exc
        if st not in _ALLOWED:
            raise TenantGateUnavailable(f"tenant-service returned an unknown status {st!r}")
        self._cache[tenant] = (now + self._ttl, st)
        return st
