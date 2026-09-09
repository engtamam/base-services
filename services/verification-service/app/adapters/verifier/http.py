"""Generic HTTP verifier — one adapter, any provider, all of it config.

Nafath, Wathq, Companies House, VIES, BankID, a provider that does not exist yet: none of them are
code here. Each is a CONFIG PROFILE saying where to POST, how to authenticate, what to send, and
where in the response the answer lives.

Two reasons, and the second is the one that decided it.

**Plug-and-play.** A customer with a provider we have never heard of wires it without touching this
repo — the same property that makes PRIVACY_TARGETS work for a customer's own CRM.

**Honesty.** We do not have verified API contracts for Nafath or Wathq. Writing `nafath.py` with
invented endpoints and field names would produce a file that looks authoritative, passes every test
we write against our own invention, and fails the first time it meets the real API. That is exactly
the defect the hand-written OpenAPI specs had. A config profile makes the customer supply the
contract they actually hold, which is the only person who can.

**The response mapping is the load-bearing part.** A provider that answers 200 with
`{"data": {"status": "NOT_FOUND"}}` has REFUSED the claim; the same 200 with an unparseable body
tells us nothing. Getting that backwards is how a verifier starts approving everyone, so:

    the verdict field is missing, unreadable, or unrecognised  ->  ERROR, never VERIFIED
    the verdict matches the configured success value           ->  VERIFIED
    the verdict matches a configured refusal value             ->  REFUSED
    the verdict is a value nobody configured                   ->  ERROR

Unrecognised means unknown. It never falls through to yes.
"""
from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Any

from app.ports.verifier import ERROR, PENDING, REFUSED, VERIFIED, Claim, Outcome


class HttpVerifier:
    """Config-mapped provider client.

    profile keys:
        url               where to POST (config only — never from a request, so no SSRF surface)
        auth              "bearer:TOKEN" | "header:X-Api-Key:TOKEN" | "" — TOKEN is a SECRET NAME
        request           "field={claim}&other=literal" — what to send
        verdict_path      dotted path into the response, e.g. "data.status"
        verdict_ok        the value meaning VERIFIED, e.g. "VERIFIED"
        verdict_refused   comma list meaning REFUSED, e.g. "NOT_FOUND,MISMATCH"
        verdict_pending   comma list meaning PENDING (async), e.g. "AWAITING_USER"
        reference_path    dotted path to the provider's own id, for the audit trail
    """

    driver = "http"

    def __init__(self, name: str, profile: dict[str, str], token: str = "",
                 timeout: int = 15) -> None:
        self._name = name
        self._p = profile
        self._token = token
        self._timeout = timeout

    async def verify(self, claim: Claim) -> Outcome:
        url = self._p.get("url", "")
        if not url:
            return self._error("no url configured for this provider")

        body = self._render_request(claim)
        req = urllib.request.Request(url, data=body, method="POST",
                                     headers=self._headers())

        def _fetch():                                        # blocking urllib OFF the event loop
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                return resp.status, resp.read()
        try:
            status, raw = await asyncio.to_thread(_fetch)
        except urllib.error.HTTPError as exc:
            # An HTTP error is US failing to ask, not the provider refusing. A 500 from the
            # registry does not mean the company is not registered.
            return self._error(f"provider returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return self._error(f"provider unreachable: {exc}")

        if not 200 <= status < 300:
            return self._error(f"provider returned HTTP {status}")

        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            return self._error(f"HTTP {status} with an unreadable body")
        if not isinstance(payload, dict):
            return self._error(f"HTTP {status} body was not an object")

        return self._read_verdict(payload)

    # ── request ─────────────────────────────────────────────────────────────
    def _render_request(self, claim: Claim) -> bytes:
        """Build the provider's payload from the configured template.

        `{claim}` is substituted with the raw identifier, which exists only for this call. Extra
        fields let a provider that needs a date of birth or a name get them without code.
        """
        spec = self._p.get("request", "value={claim}")
        out: dict[str, Any] = {}
        for pair in spec.split("&"):
            if not pair.strip():
                continue
            key, _, value = pair.partition("=")
            if value == "{claim}":
                out[key.strip()] = claim.value
            elif value.startswith("{") and value.endswith("}"):
                out[key.strip()] = claim.extra.get(value[1:-1], "")
            else:
                out[key.strip()] = value
        return json.dumps(out).encode()

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        auth = self._p.get("auth", "")
        if not auth or not self._token:
            return headers
        scheme, _, rest = auth.partition(":")
        if scheme == "bearer":
            headers["Authorization"] = f"Bearer {self._token}"
        elif scheme == "header":
            name = rest.partition(":")[0] or "X-Api-Key"
            headers[name] = self._token
        return headers

    # ── response ────────────────────────────────────────────────────────────
    def _read_verdict(self, payload: dict) -> Outcome:
        path = self._p.get("verdict_path", "")
        raw = _dig(payload, path) if path else None
        if raw is None:
            # The provider answered something we cannot interpret. That is not a refusal and it is
            # certainly not an approval — it is us not knowing.
            return self._error(f"no verdict at {path!r} in the provider response")

        value = str(raw).strip()
        reference = str(_dig(payload, self._p.get("reference_path", "")) or "")

        if value == self._p.get("verdict_ok", "").strip():
            return Outcome(status=VERIFIED, provider=self._name, reference=reference)
        if value in _csv(self._p.get("verdict_refused", "")):
            return Outcome(status=REFUSED, provider=self._name, reference=reference,
                           detail=f"provider answered {value!r}")
        if value in _csv(self._p.get("verdict_pending", "")):
            return Outcome(status=PENDING, provider=self._name, reference=reference,
                           detail=f"provider answered {value!r}; awaiting its callback",
                           state={"reference": reference})

        # A value nobody configured. Unknown is unknown — treating it as success is how a mapping
        # typo silently turns into "everyone is verified".
        return self._error(f"provider returned an unconfigured verdict {value!r}")

    def _error(self, detail: str) -> Outcome:
        return Outcome(status=ERROR, provider=self._name, detail=detail)


def _dig(payload: dict, path: str) -> Any:
    """Follow a dotted path. Returns None rather than raising — a missing field is an unknown
    verdict, which the caller turns into ERROR."""
    if not path:
        return None
    node: Any = payload
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _csv(value: str) -> frozenset[str]:
    return frozenset(v.strip() for v in value.split(",") if v.strip())
