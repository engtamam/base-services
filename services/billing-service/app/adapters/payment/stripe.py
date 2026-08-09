"""Stripe — PaymentIntents. Verified: POST /v1/payment_intents, Bearer sk, form,
amount in MINOR units, webhook Stripe-Signature (t=..,v1=HMAC-SHA256 of 't.body')."""
from __future__ import annotations

import time

from app.adapters.billing_http import bearer, post_form
from app.adapters.payment._common import constant_eq, hmac_sha256_hex
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = {"succeeded"}


class StripeGateway:
    driver = "stripe"

    def __init__(self, api_key: str, base: str = "", webhook_secret: str = "",
                 webhook_tolerance: int = 300, **_) -> None:
        if not api_key:
            raise GatewayUnavailable("PAYMENT_API_KEY (Stripe secret key) required")
        self._h = {"Authorization": bearer(api_key)}
        self._base = (base or "https://api.stripe.com").rstrip("/")
        self._whsec = webhook_secret
        self._tolerance = int(webhook_tolerance)

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        fields = {"amount": int(amount_minor), "currency": currency.lower(),
                  "payment_method": token, "confirm": "true",
                  "description": description, "automatic_payment_methods[enabled]": "true",
                  "automatic_payment_methods[allow_redirects]": "never"}
        for k, v in (metadata or {}).items():
            fields[f"metadata[{k}]"] = v
        code, resp = post_form(f"{self._base}/v1/payment_intents", fields,
                               {**self._h, "Idempotency-Key": idempotency_key})
        if code == 0:
            raise GatewayUnavailable("Stripe unreachable")
        status = str(resp.get("status", "failed"))
        return Charge(ok=status in _OK, status="succeeded" if status in _OK else "failed",
                      provider="stripe", provider_id=str(resp.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if status in _OK else str((resp.get("last_payment_error") or {}).get("message", status)))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        fields = {"payment_intent": provider_id}
        if amount_minor is not None:
            fields["amount"] = int(amount_minor)
        code, resp = post_form(f"{self._base}/v1/refunds", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("Stripe unreachable")
        ok = str(resp.get("status", "")) in ("succeeded", "pending")
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="stripe",
                      provider_id=str(resp.get("id", "")))

    def verify_webhook(self, *, headers, body, secret):
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        sig = h.get("stripe-signature", "")
        parts = dict(p.split("=", 1) for p in sig.split(",") if "=" in p)
        expected = hmac_sha256_hex(secret or self._whsec, f"{parts.get('t','')}.".encode() + body)
        # Constant-time compare AND a freshness window: Stripe signs a timestamp `t`, and an event
        # older than the tolerance is a replay to reject even with a valid signature (a captured
        # webhook could otherwise be resent to re-trigger a paid transition). 0 disables the window.
        signed = bool(parts.get("v1")) and constant_eq(parts.get("v1", ""), expected)
        fresh = True
        if self._tolerance > 0:
            try:
                fresh = abs(int(time.time()) - int(parts.get("t", "0"))) <= self._tolerance
            except (TypeError, ValueError):
                fresh = False
        verified = signed and fresh
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        t = evt.get("type", "")
        kind = "payment.succeeded" if "succeeded" in t else "payment.failed" if "failed" in t \
            else "refund" if "refund" in t else "unknown"
        return WebhookEvent(verified=verified, type=kind,
                            provider_id=str(((evt.get("data") or {}).get("object") or {}).get("id", "")))
