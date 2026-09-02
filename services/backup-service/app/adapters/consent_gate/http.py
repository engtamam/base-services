"""HTTP gate — ask compliance-service, and believe only what it actually said.

The contract is deliberately small, and the adopting team's report confirmed why that matters:
one GET, three query parameters, one static header. There is NO caller-computed fingerprint here.
(They believed there was, having confused it with the erasure API's subject_ref HMAC — an
understandable mistake that a shipped adapter like this one removes entirely.)

    GET {COMPLIANCE_SERVICE_URL}/internal/compliance/consent-check
        ?tenant=&subject=&purpose=      ->  {"allowed": bool, "reason": str, "cached": bool}

The URL comes from CONFIG only, never from a request, so no caller can point a consent question at
a host of their choosing and be told yes.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from app.ports.consent_gate import ALLOWED, DENIED, UNKNOWN, ConsentVerdict


class HttpConsentGate:
    driver = "http"

    def __init__(self, url: str, token: str, timeout: int = 3,
                 path: str = "/internal/compliance/consent-check") -> None:
        self._url = url.rstrip("/") + path
        self._token = token
        self._timeout = timeout

    async def check(self, *, tenant: str, subject: str, purpose: str) -> ConsentVerdict:
        query = urllib.parse.urlencode(
            {"tenant": tenant, "subject": subject, "purpose": purpose})
        req = urllib.request.Request(f"{self._url}?{query}", method="GET")
        if self._token:
            req.add_header("X-Internal-Token", self._token)
        # ADD-ONLY caller identity (aud=compliance-service) when this service signs; token untouched.
        from app.core.internal_client import assertion_headers_from_env
        for _k, _v in assertion_headers_from_env(target="compliance-service").items():
            req.add_header(_k, _v)

        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                status, raw = resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            # A 4xx here is OUR bug (bad token, wrong path), not a statement about the subject.
            # Reporting it as denied would silently stop lawful processing platform-wide the day
            # someone rotates a token; reporting it as allowed would be worse. It is UNKNOWN.
            return ConsentVerdict(status=UNKNOWN, purpose=purpose,
                                  reason=f"consent-check returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return ConsentVerdict(status=UNKNOWN, purpose=purpose,
                                  reason=f"compliance-service unreachable: {exc}")

        if not 200 <= status < 300:
            return ConsentVerdict(status=UNKNOWN, purpose=purpose,
                                  reason=f"consent-check returned HTTP {status}")
        try:
            body = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return ConsentVerdict(status=UNKNOWN, purpose=purpose,
                                  reason=f"HTTP {status} with an unreadable body")
        if not isinstance(body, dict) or "allowed" not in body:
            # A 200 we cannot interpret is not permission. Treating a malformed answer as allowed
            # is how a broken deployment turns into processing nobody agreed to.
            return ConsentVerdict(status=UNKNOWN, purpose=purpose,
                                  reason=f"HTTP {status} but no 'allowed' field")

        # `is True`, not truthiness. A JSON string "false", or a stray 0/1, must not become
        # permission by way of Python's coercion rules — the only thing that means yes is yes.
        allowed = body.get("allowed") is True
        return ConsentVerdict(
            status=ALLOWED if allowed else DENIED,
            reason=str(body.get("reason") or ""),
            purpose=str(body.get("purpose") or purpose),
            cached=bool(body.get("cached")),
        )
