"""Amazon Payment Services (PayFort) — verified: POST {base}/FortAPI/paymentApi, JSON,
command=PURCHASE, amount in MINOR units, signature = SHA-256 of
    <passphrase> + concat(sorted "name=value", no separator) + <passphrase>

Auth is by signed body (no Authorization header). PURCHASE success status = "14".
Two phrases: SHA REQUEST phrase (signing requests) and SHA RESPONSE phrase (webhooks).
"""
from __future__ import annotations

import hashlib

from app.adapters.billing_http import post_json
from app.adapters.payment._common import constant_eq
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_PURCHASE_OK = "14"
_REFUND_OK = "06"


def sign(params: dict, phrase: str) -> str:
    """PayFort signature: phrase + sorted(name=value) concatenated + phrase, SHA-256 hex."""
    body = "".join(f"{k}={params[k]}" for k in sorted(params) if k != "signature")
    return hashlib.sha256(f"{phrase}{body}{phrase}".encode()).hexdigest()


class ApsGateway:
    driver = "aps"

    def __init__(self, api_key: str, base: str = "", extra_id: str = "", extra_secret: str = "",
                 webhook_secret: str = "", **_) -> None:
        # api_key = access_code, extra_id = merchant_identifier,
        # extra_secret = SHA request phrase, webhook_secret = SHA response phrase
        if not (api_key and extra_id and extra_secret):
            raise GatewayUnavailable(
                "PAYMENT_API_KEY (access_code) + PAYMENT_EXTRA_ID (merchant_identifier) "
                "+ PAYMENT_EXTRA_SECRET (SHA request phrase) required")
        self._access, self._merchant = api_key, extra_id
        self._req_phrase, self._resp_phrase = extra_secret, webhook_secret
        self._base = (base or "https://sbpaymentservices.payfort.com").rstrip("/")

    def _post(self, params: dict) -> dict:
        params["signature"] = sign(params, self._req_phrase)
        code, resp = post_json(f"{self._base}/FortAPI/paymentApi", params)
        if code == 0:
            raise GatewayUnavailable("APS/PayFort unreachable")
        return resp

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        resp = self._post({"command": "PURCHASE", "access_code": self._access,
                           "merchant_identifier": self._merchant, "merchant_reference": idempotency_key,
                           "amount": str(int(amount_minor)), "currency": currency.upper(),
                           "language": "en", "token_name": token})
        ok = str(resp.get("status", "")) == _PURCHASE_OK
        return Charge(ok=ok, status="succeeded" if ok else "failed", provider="aps",
                      provider_id=str(resp.get("fort_id", "")), amount_minor=int(amount_minor),
                      currency=currency, error="" if ok else str(resp.get("response_message", "declined")))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        resp = self._post({"command": "REFUND", "access_code": self._access,
                           "merchant_identifier": self._merchant, "fort_id": provider_id,
                           "amount": str(int(amount_minor or 0)), "currency": currency.upper(), "language": "en"})
        ok = str(resp.get("status", "")) == _REFUND_OK
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="aps",
                      provider_id=str(resp.get("fort_id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            return WebhookEvent(verified=False)
        expected = sign(evt, secret or self._resp_phrase)     # response phrase signs the feedback
        verified = constant_eq(str(evt.get("signature", "")), expected)
        status = str(evt.get("status", ""))
        kind = "payment.succeeded" if status == _PURCHASE_OK else \
            "refund" if status == _REFUND_OK else "payment.failed"
        # Refund amount (FIN-015): APS amounts are MINOR units (a string), the REFUND feedback carries
        # this refund's `amount`. Verified 2026-09-26 against paymentservices.amazon.com. 0 → full-refund.
        refunded = 0
        if kind == "refund":
            try:
                refunded = int(str(evt.get("amount", "0")).strip() or 0)
            except Exception:  # noqa: BLE001
                refunded = 0
        return WebhookEvent(verified=verified, type=kind, provider_id=str(evt.get("fort_id", "")),
                            refunded_minor=refunded)
