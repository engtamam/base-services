"""HTTP channel-pricing client — reads operations-service's per-tenant WhatsApp policy, briefly cached.

POST {operations}/internal/operations/whatsapp-channel-pricing {tenant} over X-Internal-Token →
{included_messages_per_period:int, overage_halalas_per_message:int}. This is POLICY only (an allowance
and an overage price); it never decides one specific send — that decision is made at the send path where
the live message count is, avoiding a stale-count race. Cached per tenant for a short TTL because the
policy changes rarely; a fetch failure is surfaced as `None`, and the GATE's posture (fail_closed /
fail_open) decides what an unknown policy means for the send, rather than this client inventing a price.
"""
from __future__ import annotations

import asyncio
import json as _json
import time

from app.adapters.notify_http import ok, post_json
from app.core.internal_client import assertion_headers


class HttpChannelPricing:
    def __init__(self, base_url: str, internal_token: str, *, ttl_seconds: int = 300,
                 clock=time.time, settings=None) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token_headers = {"X-Internal-Token": internal_token}
        self._settings = settings           # for the caller assertion; None => token-only (tests)
        self._ttl = max(0, ttl_seconds)
        self._clock = clock
        self._cache: dict[str, tuple[float, dict]] = {}   # tenant -> (expires_at, policy)

    def _headers(self) -> dict[str, str]:
        if self._settings is None:
            return dict(self._token_headers)
        return {**self._token_headers, **assertion_headers(self._settings, target="operations")}

    async def get(self, tenant: str) -> dict | None:
        """The tenant's {included_messages_per_period, overage_halalas_per_message}, or None if it
        could not be read. Both fields default to 0 when the response omits them (0 included = no free
        tier; 0 overage = free past the cap) — the contract operations-service publishes."""
        now = self._clock()
        hit = self._cache.get(tenant)
        if hit is not None and hit[0] > now:
            return hit[1]
        status, body = await asyncio.to_thread(
            post_json, f"{self._base}/internal/operations/whatsapp-channel-pricing",
            {"tenant": tenant}, self._headers())
        if not ok(status):
            return None                          # unknown policy — the gate posture decides
        try:
            raw = _json.loads(body)
        except (ValueError, TypeError):
            return None
        policy = {
            "included_messages_per_period": int(raw.get("included_messages_per_period", 0) or 0),
            "overage_halalas_per_message": int(raw.get("overage_halalas_per_message", 0) or 0),
        }
        self._cache[tenant] = (now + self._ttl, policy)
        return policy
