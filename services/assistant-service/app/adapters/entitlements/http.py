"""HTTP resolver — ask the billing/operations tier what a tenant holds, believe only what it says.

    GET {ENTITLEMENTS_RESOLVER_URL}/internal/entitlements?tenant=   ->  {"held": ["feature.x", ...]}

The URL is CONFIG only, never a request, so no caller can point the entitlement question at a host of
its choosing. A failure (unreachable, 4xx/5xx, unparseable) resolves to the EMPTY set — fail-closed to
invisible — and NEVER raises: a resolver hiccup must degrade to "detailed KB hidden", not an outage on
every ask. The response shape is intentionally tiny; adapt the path via ENTITLEMENTS_RESOLVER_PATH.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class HttpEntitlements:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 3,
                 path: str = "/internal/entitlements") -> None:
        self._url = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout

    async def held(self, tenant: str) -> frozenset[str]:
        if not tenant:
            return frozenset()
        query = urllib.parse.urlencode({"tenant": tenant})
        req = urllib.request.Request(f"{self._url}?{query}", method="GET")
        if self._token:
            req.add_header("X-Internal-Token", self._token)
        from app.core.internal_client import assertion_headers_from_env
        for _k, _v in assertion_headers_from_env(target="billing-service").items():
            req.add_header(_k, _v)
        try:
            def _send():  # blocking urllib OFF the event loop
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    return resp.status, resp.read()
            status, raw = await asyncio.to_thread(_send)
        except (urllib.error.URLError, OSError):
            return frozenset()                       # unreachable → fail-closed to invisible, never raise
        if not 200 <= status < 300:
            return frozenset()
        try:
            data = json.loads(raw.decode("utf-8", "replace"))
            keys = data.get("held") or []
            return frozenset(str(k).strip() for k in keys if str(k).strip())
        except (ValueError, AttributeError, TypeError):
            return frozenset()                       # unparseable → fail-closed
