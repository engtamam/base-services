"""HttpTenantGate — the tenant's lifecycle status from tenant-service, cached.

    GET {TENANT_SERVICE_URL}/internal/tenants/{id}  ->  {"status": "active|suspended|deleted", ...}

This sits on the HOT PATH: every authenticated request through the gateway asks "is this tenant still
allowed?". An un-cached call would make tenant-service a hard synchronous dependency of every request,
so answers are cached in the shared cache port for TENANT_STATUS_TTL_SECONDS. That TTL is also the
enforcement lag: suspend a tenant and every gateway pod refuses it within the TTL.

A DELETED tenant answers 200 with status "deleted" (tenant-service tombstones, it does not 404), so a
suspend/delete is a definite negative status, not a lookup miss. A genuine 404 means the tenant does
not exist -> "unknown". Any other failure raises TenantGateUnavailable; the caller decides fail
open/closed. The URL is config, never derived from a request: a caller who could point it at a host
they control could answer "active" for a tenant the platform suspended.
"""
from __future__ import annotations

import httpx

from app.core.internal_client import assertion_headers
from app.ports.errors import ConfigError
from app.ports.tenant_gate import DELETED, SUSPENDED, UNKNOWN, TenantGateUnavailable

_ALLOWED = {"active", SUSPENDED, DELETED}


class HttpTenantGate:
    driver = "tenant"

    def __init__(self, base_url: str, internal_token: str, *, cache, settings,
                 timeout_ms: int, ttl_seconds: int, token_header: str = "X-Internal-Token") -> None:
        if not base_url:
            raise ConfigError(
                "TENANT_GATE_DRIVER=tenant requires TENANT_SERVICE_URL — status enforcement cannot "
                "consult a tenant authority that is not configured")
        if not internal_token:
            raise ConfigError(
                "TENANT_GATE_DRIVER=tenant requires INTERNAL_SERVICE_TOKEN to call "
                "/internal/tenants/{id}")
        self._httpx = httpx
        self._url = base_url.rstrip("/") + "/internal/tenants/"
        self._token = internal_token
        self._cache = cache
        self._settings = settings
        self._timeout = timeout_ms / 1000
        self._ttl = ttl_seconds
        self._token_header = token_header

    def _key(self, tenant: str) -> str:
        return f"tenant-status:{tenant}"

    async def status(self, tenant: str) -> str:
        key = self._key(tenant)
        try:
            cached = await self._cache.get(key)
        except Exception:
            cached = None
        if cached:
            return cached

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
            await self._store(key, UNKNOWN)
            return UNKNOWN
        if r.status_code != 200:
            raise TenantGateUnavailable(f"tenant-service answered {r.status_code}")
        try:
            st = str(r.json().get("status") or "").lower()
        except Exception as exc:
            raise TenantGateUnavailable("tenant-service returned a malformed body") from exc
        if st not in _ALLOWED:
            raise TenantGateUnavailable(f"tenant-service returned an unknown status {st!r}")
        await self._store(key, st)
        return st

    async def _store(self, key: str, value: str) -> None:
        try:
            await self._cache.set(key, value, self._ttl)
        except Exception:
            pass
