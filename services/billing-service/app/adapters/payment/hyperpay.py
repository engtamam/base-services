"""HyperPay (KSA, OPP COPYandPAY) — verified: POST {base}/v1/payments, Bearer access
token, FORM, entityId + amount in MAJOR units (decimal) + paymentType=DB. Refund =
POST /v1/payments/{id} with paymentType=RF. Result code 000.000.* = success."""
from __future__ import annotations

import re

from app.adapters.billing_http import bearer, post_form
from app.adapters.payment._common import major
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = re.compile(r"^(000\.000\.|000\.100\.1|000\.[36])")


class HyperPayGateway:
    driver = "hyperpay"

    def __init__(self, api_key: str, base: str = "", extra_id: str = "", webhook_secret: str = "", **_) -> None:
        if not (api_key and extra_id):
            raise GatewayUnavailable("PAYMENT_API_KEY + PAYMENT_EXTRA_ID (HyperPay entityId) required")
        self._h = {"Authorization": bearer(api_key)}
        self._base = (base or "https://eu-test.oppwa.com").rstrip("/")
        self._entity = extra_id
        self._whsec = webhook_secret

    def _result_ok(self, resp: dict) -> tuple[bool, str]:
        code = str((resp.get("result") or {}).get("code", ""))
        return bool(_OK.match(code)), code

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        fields = {"entityId": self._entity, "amount": major(amount_minor, currency), "currency": currency.upper(),
                  "paymentType": "DB", "registrationId": token, "merchantTransactionId": idempotency_key}
        code, resp = post_form(f"{self._base}/v1/payments", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("HyperPay unreachable")
        ok, rc = self._result_ok(resp)
        return Charge(ok=ok, status="succeeded" if ok else "failed", provider="hyperpay",
                      provider_id=str(resp.get("id", "")), amount_minor=int(amount_minor),
                      currency=currency, error="" if ok else rc)

    def refund(self, *, provider_id, amount_minor=None):
        fields = {"entityId": self._entity, "paymentType": "RF"}
        if amount_minor is not None:
            fields["amount"] = major(amount_minor)
        code, resp = post_form(f"{self._base}/v1/payments/{provider_id}", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("HyperPay unreachable")
        ok, _rc = self._result_ok(resp)
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="hyperpay",
                      provider_id=str(resp.get("id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        # HyperPay webhooks are AES-encrypted; decryption is deployment-specific.
        # We surface the event unverified for the caller to reconcile via GET status.
        return WebhookEvent(verified=False, type="unknown")
