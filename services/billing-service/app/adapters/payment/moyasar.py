"""Moyasar (KSA) — verified: POST /v1/payments, Basic auth (secret key : empty),
amount in MINOR units (halalas), refund POST /v1/payments/{id}/refund. Webhook carries
a secret_token to compare against the configured value."""
from __future__ import annotations

from app.adapters.billing_http import basic_auth, post_form
from app.adapters.payment._common import constant_eq
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = {"paid", "authorized"}


class MoyasarGateway:
    driver = "moyasar"

    def __init__(self, api_key: str, base: str = "", webhook_secret: str = "", **_) -> None:
        if not api_key:
            raise GatewayUnavailable("PAYMENT_API_KEY (Moyasar secret key) required")
        self._h = {"Authorization": basic_auth(api_key, "")}
        self._base = (base or "https://api.moyasar.com").rstrip("/")
        self._whsec = webhook_secret

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        fields = {"amount": int(amount_minor), "currency": currency.upper(),
                  "description": description, "source[type]": "token", "source[token]": token}
        code, resp = post_form(f"{self._base}/v1/payments", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable")
        status = str(resp.get("status", "failed"))
        return Charge(ok=status in _OK, status="succeeded" if status in _OK else "failed",
                      provider="moyasar", provider_id=str(resp.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if status in _OK else str((resp.get("source") or {}).get("message", status)))

    def refund(self, *, provider_id, amount_minor=None):
        fields = {} if amount_minor is None else {"amount": int(amount_minor)}
        code, resp = post_form(f"{self._base}/v1/payments/{provider_id}/refund", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable")
        ok = str(resp.get("status", "")) in ("refunded", "paid")
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="moyasar",
                      provider_id=str(resp.get("id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        verified = constant_eq(str(evt.get("secret_token", "")), secret or self._whsec)
        status = str((evt.get("data") or {}).get("status", evt.get("type", "")))
        kind = "payment.succeeded" if status in _OK or "paid" in status else \
            "refund" if "refund" in status else "payment.failed"
        return WebhookEvent(verified=verified, type=kind,
                            provider_id=str((evt.get("data") or {}).get("id", "")))
