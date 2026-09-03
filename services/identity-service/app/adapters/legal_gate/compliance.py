"""ComplianceLegalGate — outstanding legal acceptances from compliance-service, at mint time.

    GET {COMPLIANCE_URL}/internal/compliance/legal/outstanding?tenant=&subject=  -> {"outstanding": [...]}

Modeled on caps_resolver/authz.py, and it sits on the SAME hot path (login, tenant switch, every
refresh). The differences from AuthzCaps are deliberate and load-bearing:

1. TTL DEFAULTS TO 0 (no cache). Caching "outstanding" per (subject, tenant) would keep reporting a
   document as owed for up to the TTL AFTER the subject accepted it — re-looping a user who just
   accepted straight back into the block. Logins are far rarer than authz mints, so correctness of
   the accept→clear cycle wins over shaving a call. An operator may set a small TTL to dedupe bursts.

2. IT DOES NOT DICTATE FAIL-CLOSED. Raising on an unreachable authority is the caller's to interpret
   (IDENTITY_LEGAL_GATE_FAIL_MODE): a consent check that can 503 every login on a compliance blip is
   its own outage. What it must NOT do is return [] on an error — that is a silent disable.

3. THE URL IS CONFIG, NEVER REQUEST-DERIVED. Identity holds the internal token; a caller who could
   point COMPLIANCE_URL at a host they control would be told "owes nothing" for everyone.
"""
from __future__ import annotations

import time

from app.adapters.lazy import load
from app.ports.errors import ConfigError
from app.ports.legal_gate import LegalGateUnavailable


class ComplianceLegalGate:
    driver = "compliance"

    def __init__(self, base_url: str, internal_token: str, *,
                 timeout_ms: int, ttl_seconds: int, user_agent: str = "",
                 path: str, token_header: str, outstanding_field: str,
                 subject_param: str, tenant_param: str) -> None:
        # Every part of the wire is config (path, token header, response field, param names), so a
        # platform behind a renaming gateway swaps env vars, not code. The semantics are fixed:
        # subject+tenant go in, the owed-documents list comes out, and an unreachable authority
        # raises rather than inventing an answer.
        if not base_url:
            raise ConfigError(
                "IDENTITY_LEGAL_ENFORCEMENT is on but COMPLIANCE_URL is unset — legal acceptance "
                "cannot be enforced against an authority that is not configured")
        if not internal_token:
            raise ConfigError(
                "IDENTITY_LEGAL_ENFORCEMENT is on but COMPLIANCE_INTERNAL_TOKEN is unset — "
                "/internal/compliance/legal/outstanding is behind the internal token")
        self._httpx = load("httpx", "legalgate")
        self._url = base_url.rstrip("/") + "/" + path.lstrip("/")
        self._token = internal_token
        self._timeout = timeout_ms / 1000
        self._ttl = ttl_seconds
        self._ua = user_agent
        self._token_header = token_header
        self._field = outstanding_field
        self._subject_param = subject_param
        self._tenant_param = tenant_param
        self._cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._token_provider = None

    def set_token_provider(self, provider) -> None:
        """An async callable returning the current internal token (rotation-aware, like AuthzCaps)."""
        self._token_provider = provider

    async def _current_token(self) -> str:
        if self._token_provider is None:
            return self._token
        return await self._token_provider()

    async def outstanding(self, *, subject: str, tenant: str) -> list[dict]:
        key = (subject, tenant)
        if self._ttl > 0:
            hit = self._cache.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]

        headers = {self._token_header: await self._current_token()}
        if self._ua:
            headers["User-Agent"] = self._ua
        try:
            async with self._httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.get(self._url, headers=headers, params={
                    self._subject_param: subject, self._tenant_param: tenant})
            if r.status_code != 200:
                raise LegalGateUnavailable(
                    f"compliance-service answered {r.status_code} resolving outstanding legal")
            owed = r.json().get(self._field)
            if not isinstance(owed, list):
                # A malformed body is not "owes nothing" — reading a broken answer as an empty list
                # is exactly how a parse bug silently disables the whole gate.
                raise LegalGateUnavailable("compliance-service returned a malformed outstanding body")
        except LegalGateUnavailable:
            raise
        except Exception as exc:                     # network, DNS, timeout, TLS, bad JSON
            raise LegalGateUnavailable(
                f"compliance-service unreachable: {exc.__class__.__name__}") from exc

        owed = [dict(d) if isinstance(d, dict) else {"doc_id": str(d)} for d in owed]
        if self._ttl > 0:
            self._cache[key] = (time.monotonic() + self._ttl, owed)
        return owed
