"""HTTP identity lookup — ask verification-service for a PURPOSE-SCOPED uniqueness token.

The token is HMAC(PII_SALT, "onboarding-grant|<claim_ref>"), derived and returned by
verification-service. It is unique per verified identity (so a welcome grant issues once per real
person, across tenants and across deletion) but is NEVER the claim_ref itself — verification refuses
to hand that out, because the stable hash would let this service correlate the same person across
tenants. We only ever see a one-way, purpose-bound token we can use for grant-uniqueness and nothing
else.

Config-mapped host (never caller-influenced). Any failure — unreachable, non-200, unreadable — yields
"" so the grant fails CLOSED (withheld), never issued on an unproven claim of uniqueness.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.parse
import urllib.request


class HttpIdentityLookup:
    driver = "http"

    def __init__(self, base_url: str, token: str, claim_type: str, timeout: int = 5) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._claim_type = claim_type
        self._timeout = timeout

    async def claim_ref_for(self, tenant: str, subject: str) -> str:
        q = lambda v: urllib.parse.quote(str(v), safe="")  # noqa: E731
        url = (f"{self._base}/internal/verification/identity-grant-token"
               f"?tenant={q(tenant)}&subject={q(subject)}&claim_type={q(self._claim_type)}")
        req = urllib.request.Request(
            url, method="GET",
            headers={"X-Internal-Token": self._token} if self._token else {})
        try:
            def _send():  # blocking urllib OFF the event loop
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    return resp.read()
            raw = await asyncio.to_thread(_send)
        except (urllib.error.URLError, OSError):
            return ""                                  # unreachable → fail closed (grant withheld)
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return ""
        token = payload.get("token") if isinstance(payload, dict) else ""
        return str(token or "")
