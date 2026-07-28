"""PayPal — Orders v2. OAuth2 client-credentials -> capture an approved order.

Verified flow: POST /v1/oauth2/token (Basic client:secret) -> access token; the buyer
approves an order in the UI and billing passes the orderID as `token`; we POST
/v2/checkout/orders/{id}/capture. Amount is MAJOR units (decimal string)."""
from __future__ import annotations

from app.adapters.billing_http import basic_auth, bearer, post_form, post_json
from app.adapters.payment._common import major
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent


class PayPalGateway:
    driver = "paypal"

    def __init__(self, api_key: str, base: str = "", extra_id: str = "", webhook_secret: str = "", **_) -> None:
        # api_key = client_id, extra_id = client_secret
        if not (api_key and extra_id):
            raise GatewayUnavailable("PAYMENT_API_KEY (client_id) + PAYMENT_EXTRA_ID (client_secret) required")
        self._client, self._secret = api_key, extra_id
        self._base = (base or "https://api-m.sandbox.paypal.com").rstrip("/")
        self._whsec = webhook_secret

    def _token(self) -> str:
        code, resp = post_form(f"{self._base}/v1/oauth2/token", {"grant_type": "client_credentials"},
                               {"Authorization": basic_auth(self._client, self._secret)})
        if code == 0 or not resp.get("access_token"):
            raise GatewayUnavailable("PayPal token unreachable")
        return str(resp["access_token"])

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        # `token` is an approved PayPal order id -> capture it.
        code, resp = post_json(f"{self._base}/v2/checkout/orders/{token}/capture", {},
                               {"Authorization": bearer(self._token()), "PayPal-Request-Id": idempotency_key})
        if code == 0:
            raise GatewayUnavailable("PayPal unreachable")
        ok = str(resp.get("status", "")) == "COMPLETED"
        return Charge(ok=ok, status="succeeded" if ok else "failed", provider="paypal",
                      provider_id=str(resp.get("id", token)), amount_minor=int(amount_minor),
                      currency=currency, error="" if ok else str(resp.get("status", "failed")))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        body = {} if amount_minor is None else {
            "amount": {"value": major(amount_minor, currency), "currency_code": currency.upper()}}
        code, resp = post_json(f"{self._base}/v2/payments/captures/{provider_id}/refund", body,
                               {"Authorization": bearer(self._token())})
        if code == 0:
            raise GatewayUnavailable("PayPal unreachable")
        ok = str(resp.get("status", "")) in ("COMPLETED", "PENDING")
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="paypal",
                      provider_id=str(resp.get("id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        # PayPal verifies via an API call (transmission id/sig/cert) — deployment-specific.
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        t = str(evt.get("event_type", ""))
        kind = "payment.succeeded" if "COMPLETED" in t or "CAPTURE.COMPLETED" in t else \
            "refund" if "REFUND" in t else "payment.failed" if "DENIED" in t or "DECLINED" in t else "unknown"
        return WebhookEvent(verified=False, type=kind,
                            provider_id=str(((evt.get("resource") or {})).get("id", "")))
