"""HTTP WalletClient — talks to wallet-service's internal tier over X-Internal-Token.

reserve → POST /internal/wallet/reserve {tenant, net, source, reason}; a 402 is the money refusal
(INSUFFICIENT), any other non-2xx is UNAVAILABLE (the gate's posture decides what that means for the
send). confirm/release → POST /internal/wallet/{confirm,release} {hold_id}. The stdlib POST is
synchronous, so each call is run off the event loop by the gate; the host is fixed config, never
caller input. Headers carry the shared token AND, when this service signs, a caller assertion for
wallet-service (aud=wallet-service).
"""
from __future__ import annotations

import asyncio
import json as _json

from app.adapters.notify_http import ok, post_json
from app.core.internal_client import assertion_headers
from app.ports.wallet import INSUFFICIENT, UNAVAILABLE, ReserveResult


class HttpWallet:
    driver = "http"

    def __init__(self, base_url: str, internal_token: str, settings=None) -> None:
        self._base = (base_url or "").rstrip("/")
        self._token_headers = {"X-Internal-Token": internal_token}
        self._settings = settings           # for the caller assertion; None => token-only (tests)

    def _headers(self) -> dict[str, str]:
        # Token (always) + a fresh-enough caller assertion (when signing is configured), per call so
        # the short-TTL assertion is never stale. assertion_headers caches, so this is cheap.
        if self._settings is None:
            return dict(self._token_headers)
        return {**self._token_headers, **assertion_headers(self._settings, target="wallet-service")}

    async def reserve(self, *, tenant: str, amount: int, source: str, reason: str) -> ReserveResult:
        status, body = await asyncio.to_thread(
            post_json, f"{self._base}/internal/wallet/reserve",
            {"tenant": tenant, "net": int(amount), "source": source, "reason": reason}, self._headers())
        if ok(status):
            try:
                hold_id = str(_json.loads(body).get("hold_id", "") or "")
            except (ValueError, AttributeError):
                hold_id = ""
            # A 2xx with no hold_id is a broken contract, not a success — treat as UNAVAILABLE so the
            # posture decides, rather than sending on a reservation we cannot later confirm or release.
            return ReserveResult(hold_id=hold_id) if hold_id else ReserveResult(refused=UNAVAILABLE)
        if status == 402:                       # wallet-service maps InsufficientFunds → 402
            return ReserveResult(refused=INSUFFICIENT)
        return ReserveResult(refused=UNAVAILABLE)

    async def confirm(self, *, hold_id: str) -> bool:
        status, _ = await asyncio.to_thread(
            post_json, f"{self._base}/internal/wallet/confirm", {"hold_id": hold_id}, self._headers())
        return ok(status)

    async def release(self, *, hold_id: str) -> bool:
        status, _ = await asyncio.to_thread(
            post_json, f"{self._base}/internal/wallet/release", {"hold_id": hold_id}, self._headers())
        return ok(status)
