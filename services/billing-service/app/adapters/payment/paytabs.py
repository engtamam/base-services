"""PayTabs (KSA/MENA) — verified: POST /payment/request, Authorization: <server_key>
header, JSON, profile_id + tran_type + cart_amount in MAJOR units. Refund =
tran_type 'refund' with tran_ref. Webhook 'signature' header = HMAC-SHA256 of the body."""
from __future__ import annotations

from app.adapters.billing_http import post_json
from app.adapters.payment._common import constant_eq, hmac_sha256_hex, major
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent


class PayTabsGateway:
    driver = "paytabs"

    def __init__(self, api_key: str, base: str = "", extra_id: str = "", webhook_secret: str = "", **_) -> None:
        if not (api_key and extra_id):
            raise GatewayUnavailable("PAYMENT_API_KEY (server key) + PAYMENT_EXTRA_ID (profile_id) required")
        self._h = {"Authorization": api_key}
        self._base = (base or "https://secure.paytabs.com").rstrip("/")
        self._profile = extra_id
        self._whsec = webhook_secret or api_key

    def _accepted(self, resp: dict) -> bool:
        return str((resp.get("payment_result") or {}).get("response_status", "")) in ("A", "H")

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        body = {"profile_id": self._profile, "tran_type": "sale", "tran_class": "ecom",
                "cart_id": idempotency_key, "cart_currency": currency.upper(),
                "cart_amount": float(major(amount_minor, currency)), "cart_description": description or "subscription",
                "payment_token": token}
        code, resp = post_json(f"{self._base}/payment/request", body, self._h)
        if code == 0:
            raise GatewayUnavailable("PayTabs unreachable")
        ok = self._accepted(resp)
        return Charge(ok=ok, status="succeeded" if ok else "failed", provider="paytabs",
                      provider_id=str(resp.get("tran_ref", "")), amount_minor=int(amount_minor),
                      currency=currency,
                      error="" if ok else str((resp.get("payment_result") or {}).get("response_message", "declined")))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        body = {"profile_id": self._profile, "tran_type": "refund", "tran_class": "ecom",
                "tran_ref": provider_id, "cart_id": provider_id, "cart_currency": currency.upper(),
                "cart_amount": float(major(amount_minor or 0, currency)), "cart_description": "refund"}
        code, resp = post_json(f"{self._base}/payment/request", body, self._h)
        if code == 0:
            raise GatewayUnavailable("PayTabs unreachable")
        ok = self._accepted(resp)
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="paytabs",
                      provider_id=str(resp.get("tran_ref", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        # PayTabs' IPN/webhook signs the ENTIRE raw JSON body with the profile Server Key (the
        # form-field digest is a DIFFERENT scheme, used only for the browser return URL). Accept the
        # header under either spelling, case-insensitively.
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        sig = h.get("signature", "") or h.get("x-paytabs-signature", "")
        verified = constant_eq(sig, hmac_sha256_hex(secret or self._whsec, body))
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        status = str((evt.get("payment_result") or {}).get("response_status", ""))
        kind = "payment.succeeded" if status in ("A", "H") else "payment.failed"
        return WebhookEvent(verified=verified, type=kind, provider_id=str(evt.get("tran_ref", "")))
