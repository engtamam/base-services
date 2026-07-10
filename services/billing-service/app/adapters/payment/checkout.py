"""Checkout.com — verified: POST /payments, Bearer sk, JSON, amount in MINOR units,
source {type: token, token}. Webhook Cko-Signature = HMAC-SHA256 hex of the body."""
from __future__ import annotations

from app.adapters.billing_http import bearer, post_json
from app.adapters.payment._common import constant_eq, hmac_sha256_hex
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = {"Authorized", "Captured", "Paid"}


class CheckoutGateway:
    driver = "checkout"

    def __init__(self, api_key: str, base: str = "", webhook_secret: str = "", **_) -> None:
        if not api_key:
            raise GatewayUnavailable("PAYMENT_API_KEY (Checkout.com secret key) required")
        self._h = {"Authorization": bearer(api_key)}
        self._base = (base or "https://api.checkout.com").rstrip("/")
        self._whsec = webhook_secret

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        body = {"amount": int(amount_minor), "currency": currency.upper(),
                "reference": idempotency_key, "description": description,
                "source": {"type": "token", "token": token}, "metadata": metadata or {}}
        code, resp = post_json(f"{self._base}/payments", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Checkout.com unreachable")
        status = str(resp.get("status", "Declined"))
        return Charge(ok=status in _OK, status="succeeded" if status in _OK else "failed",
                      provider="checkout", provider_id=str(resp.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if status in _OK else str(resp.get("response_summary", status)))

    def refund(self, *, provider_id, amount_minor=None):
        body = {} if amount_minor is None else {"amount": int(amount_minor)}
        code, resp = post_json(f"{self._base}/payments/{provider_id}/refunds", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Checkout.com unreachable")
        ok = 200 <= code < 300
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="checkout",
                      provider_id=str(resp.get("action_id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        verified = constant_eq(headers.get("cko-signature", ""), hmac_sha256_hex(secret or self._whsec, body))
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        t = str(evt.get("type", ""))
        kind = "payment.succeeded" if t in ("payment_approved", "payment_captured") else \
            "refund" if "refund" in t else "payment.failed" if "declined" in t or "failed" in t else "unknown"
        return WebhookEvent(verified=verified, type=kind,
                            provider_id=str(((evt.get("data") or {})).get("id", "")))
