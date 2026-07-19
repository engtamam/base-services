"""HTTP — ask compliance-service for the held refs of one tenant.

    GET {COMPLIANCE_SERVICE_URL}/internal/compliance/held-refs?tenant=
        -> {"refs": ["sub_..."], "count": N, "complete": bool}

Everything that can go wrong returns None, and None means DO NOT PURGE. That is the opposite of
the usual convention in this codebase, where an unreachable dependency degrades to "carry on" —
and it is deliberate. Carrying on here means deleting audit records that may be under legal hold,
which is not recoverable and is the exact thing a court would ask about.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any


class HttpHoldRefs:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 10,
                 path: str = "/internal/compliance/held-refs") -> None:
        self._url = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout

    async def for_tenant(self, tenant: str) -> dict[str, Any] | None:
        query = urllib.parse.urlencode({"tenant": tenant})
        req = urllib.request.Request(f"{self._url}?{query}", method="GET")
        if self._token:
            req.add_header("X-Internal-Token", self._token)
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                status, raw = resp.status, resp.read()
        except (urllib.error.HTTPError, urllib.error.URLError, OSError):
            return None
        if not 200 <= status < 300:
            return None
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return None
        if not isinstance(body, dict) or "refs" not in body:
            return None
        # `complete` must be explicitly True. A response that omits it is treated as incomplete,
        # because assuming completeness is precisely the assumption that deletes held evidence.
        return {"refs": list(body.get("refs") or ()), "complete": body.get("complete") is True}
