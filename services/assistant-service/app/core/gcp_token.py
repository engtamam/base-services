"""Vertex AI OAuth token source — mint + cache a short-lived GCP access token, no vendor SDK.

Vertex AI (unlike the direct Gemini Developer API, which takes a static API key) authenticates every
call with a short-lived OAuth2 Bearer access token. The HTTP model adapter already sends
`Authorization: Bearer <credential>`, so the ONLY new thing Vertex needs is a credential resolver
that returns a FRESH access token instead of a static key. That is this module.

TWO WAYS to get the token, chosen by what is configured (both stdlib + pyjwt, which is already a
dependency — no google SDK, keeping the supply chain small):

  * service_account: sign a JWT with the service-account private key (RS256) and exchange it at the
    token endpoint for an access token (the standard two-legged OAuth flow). For anywhere with a key.
  * metadata: read the token from the GCP metadata server (Workload Identity). For running ON GCP
    (e.g. GKE in me-central2/Dammam) with NO key material to store — the better production posture.

THE TOKEN IS CACHED until just before it expires (a 60s safety margin), because minting one on every
question would sign a JWT and make a network round trip on the hot path. Refresh is proactive, so an
expired-token 401 is the rare exception, not the steady state.

Verified against Google Cloud's current docs (Sep 2026): regional endpoint
`https://{LOCATION}-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/{LOCATION}/publishers/
google/models/{MODEL}:generateContent` (v1 is the stable version); scope
`https://www.googleapis.com/auth/cloud-platform`; me-central2 (Dammam) is an available location.
"""
from __future__ import annotations

import json
import time

from app.ports.errors import ConfigError

_DEFAULT_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
_JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
_METADATA_TOKEN_URL = ("http://metadata.google.internal/computeMetadata/v1/instance/"
                       "service-accounts/default/token")
_SKEW = 60          # refresh this many seconds before expiry so an in-flight call never uses a dead token


class VertexTokenSource:
    """Resolves a cached GCP OAuth access token. `.token()` matches the adapter's KeyResolver: it
    takes no arguments and returns the Bearer value to authenticate the next call with."""

    def __init__(self, *, sa_json: str = "", scope: str = _DEFAULT_SCOPE,
                 now=time.monotonic, transport=None) -> None:
        # transport(method, url, headers, data|None) -> (status, json-dict). Injected so tests never
        # touch the network; the default is a tiny httpx call.
        self._scope = scope or _DEFAULT_SCOPE
        self._now = now
        self._transport = transport or self._httpx_transport
        self._token = ""
        self._exp = 0.0
        self._sa: dict | None = None
        if sa_json.strip():
            try:
                sa = json.loads(sa_json)
            except (ValueError, TypeError) as exc:
                raise ConfigError("MODEL_GCP_SA_JSON is not valid JSON") from exc
            for k in ("client_email", "private_key", "token_uri"):
                if not sa.get(k):
                    raise ConfigError(f"MODEL_GCP_SA_JSON is missing {k!r}")
            self._sa = sa
        # No key → metadata-server (Workload Identity) mode.

    async def token(self) -> str:
        now = self._now()
        if self._token and now < self._exp:
            return self._token
        token, ttl = await (self._from_service_account() if self._sa else self._from_metadata())
        if not token:
            raise ConfigError("could not obtain a Vertex access token")
        self._token = token
        self._exp = now + max(30, int(ttl) - _SKEW)
        return token

    # ── service-account: self-signed JWT -> token endpoint ───────────────────────
    async def _from_service_account(self) -> tuple[str, int]:
        import jwt  # pyjwt[crypto] — already a dependency; RS256 signing, never hand-rolled

        sa = self._sa or {}
        iat = int(time.time())
        assertion = jwt.encode(
            {"iss": sa["client_email"], "sub": sa["client_email"], "aud": sa["token_uri"],
             "scope": self._scope, "iat": iat, "exp": iat + 3600},
            sa["private_key"], algorithm="RS256",
            headers={"kid": sa.get("private_key_id")} if sa.get("private_key_id") else None)
        status, body = await self._transport(
            "POST", sa["token_uri"],
            {"Content-Type": "application/x-www-form-urlencoded"},
            f"grant_type={_JWT_BEARER}&assertion={assertion}")
        if status != 200 or not isinstance(body, dict):
            raise ConfigError(f"Vertex token exchange failed: HTTP {status}")
        return str(body.get("access_token", "")), int(body.get("expires_in", 3600))

    # ── metadata server: Workload Identity, no key ───────────────────────────────
    async def _from_metadata(self) -> tuple[str, int]:
        status, body = await self._transport(
            "GET", _METADATA_TOKEN_URL, {"Metadata-Flavor": "Google"}, None)
        if status != 200 or not isinstance(body, dict):
            raise ConfigError(
                "no MODEL_GCP_SA_JSON set and the GCP metadata server is unreachable — set a "
                "service-account key, or run on GCP with Workload Identity")
        return str(body.get("access_token", "")), int(body.get("expires_in", 3600))

    async def _httpx_transport(self, method, url, headers, data):  # pragma: no cover - live network
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.request(method, url, headers=headers, content=data)
        try:
            return resp.status_code, resp.json()
        except (ValueError, json.JSONDecodeError):
            return resp.status_code, None
