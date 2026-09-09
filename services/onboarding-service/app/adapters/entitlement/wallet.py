"""Wallet entitlement — the GCP-style welcome credit, issued into a promotional fund.

The money mechanics already exist in wallet-service: a `Grant` with an expiry, a promo fund kept
separate from cash, and a nightly sweep that claws back the unspent remainder. This adapter only
has to hand it the right shape and get three details right.

**The fund is promotional, never cash.** wallet-service's own comment says it plainly: refunding an
expiring gift into cash silently converts it into real money. Crediting the cash fund here would
turn a marketing gift into something withdrawable, and — in a VAT jurisdiction — into a payment the
platform owes tax on. A gift that covers a charge is a pre-VAT discount; cash is not.

**`source` is our idempotency key.** wallet-service dedupes on it, so the same welcome grant issued
twice — a retry, a race, a replayed request — credits once. Ours is already derived from a verified
identity, so it survives account deletion too.

**`expires_at` makes it a grant rather than a gift.** Without it the credit never lapses and the
"90 days" in the marketing copy is fiction. With it, wallet's sweep-grants job reclaims whatever was
not spent, which is what makes the offer bounded.
"""
from __future__ import annotations
import asyncio

import json
import urllib.error
import urllib.request

from app.ports.entitlement import ALREADY, FAILED, GRANTED, GrantRequest, Receipt

DAY_SECONDS = 86400


class WalletEntitlement:
    driver = "wallet"

    def __init__(self, url: str, token: str, fund: str = "promo", timeout: int = 10) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._fund = fund
        self._timeout = timeout

    async def issue(self, request: GrantRequest) -> Receipt:
        if not request.amount:
            # A wallet credit of zero is not a grant, and issuing it would mark the requirement
            # satisfied while the customer received nothing.
            return Receipt(status=FAILED,
                           detail=f"grant {request.kind!r} has no amount configured")

        body = {
            "tenant": request.tenant,
            "fund": self._fund,
            "amount": request.amount,
            "source": request.idempotency_key,
            "reason": f"onboarding:{request.kind}",
        }
        if request.days:
            # Relative to now, resolved by the caller's clock rather than ours — wallet stores an
            # absolute instant, and computing it here keeps the expiry in one place.
            body["expires_at"] = request.metadata.get("expires_at")

        req = urllib.request.Request(
            self._url, data=json.dumps(body).encode(), method="POST",
            headers={"Content-Type": "application/json", "X-Internal-Token": self._token})
        try:
            def _send():  # blocking urllib OFF the event loop
                with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310 - config-only host
                    return resp.status, resp.read()
            status, raw = await asyncio.to_thread(_send)
        except urllib.error.HTTPError as exc:
            return Receipt(status=FAILED, detail=f"wallet returned HTTP {exc.code}")
        except (urllib.error.URLError, OSError) as exc:
            return Receipt(status=FAILED, detail=f"wallet unreachable: {exc}")

        if not 200 <= status < 300:
            return Receipt(status=FAILED, detail=f"wallet returned HTTP {status}")
        try:
            payload = json.loads(raw or b"{}")
        except (ValueError, TypeError):
            # A 200 we cannot read is not proof the money moved, and reporting it as granted would
            # tell a customer they have credit they may not have.
            return Receipt(status=FAILED, detail="wallet returned an unreadable receipt")

        if payload.get("idempotent"):
            # wallet recognised our source and did nothing. From the caller's side the person has
            # the credit, which is why this counts as granted rather than as a failure.
            return Receipt(status=ALREADY, reference=str(payload.get("id", "")),
                           detail="wallet had already applied this grant")
        return Receipt(status=GRANTED, reference=str(payload.get("id", "")))
