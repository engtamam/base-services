"""Geidea (KSA/Egypt) — verified: POST {base}/pgw/api/v2/direct/pay, HTTP Basic auth
(merchant PUBLIC KEY as username, API PASSWORD as password), JSON, amount in MAJOR units.

Direct-pay takes a `sessionId` (created client-side with amount+currency) — that is the
`token` our port passes. Success = responseCode "000". Webhook signature is base64 HMAC.
"""
from __future__ import annotations

import base64
import hashlib
import hmac

from app.adapters.billing_http import basic_auth, post_json
from app.adapters.payment._common import constant_eq, major
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = "000"


class GeideaGateway:
    driver = "geidea"

    def __init__(self, api_key: str, base: str = "", extra_secret: str = "",
                 webhook_secret: str = "", **_) -> None:
        # api_key = merchant public key, extra_secret = API password
        if not (api_key and extra_secret):
            raise GatewayUnavailable("PAYMENT_API_KEY (public key) + PAYMENT_EXTRA_SECRET (api password) required")
        self._pubkey = api_key
        self._apipass = extra_secret
        self._h = {"Authorization": basic_auth(api_key, extra_secret)}
        self._base = (base or "https://api.merchant.geidea.net").rstrip("/")
        self._whsec = webhook_secret

    def _code(self, resp: dict) -> str:
        return str(resp.get("responseCode", ""))

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        body = {"sessionId": token, "amount": float(major(amount_minor, currency)),
                "currency": currency.upper(), "merchantReferenceId": idempotency_key}
        code, resp = post_json(f"{self._base}/pgw/api/v2/direct/pay", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Geidea unreachable")
        ok = self._code(resp) == _OK
        order = resp.get("order") or {}
        return Charge(ok=ok, status="succeeded" if ok else "failed", provider="geidea",
                      provider_id=str(order.get("orderId") or order.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if ok else str(resp.get("responseMessage", self._code(resp))))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        body = {"orderId": provider_id}
        if amount_minor is not None:
            body["amount"] = float(major(amount_minor, currency))
        code, resp = post_json(f"{self._base}/pgw/api/v1/direct/refund", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Geidea unreachable")
        ok = self._code(resp) == _OK
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="geidea",
                      provider_id=provider_id)

    def prepare_checkout(self, *, amount_minor, currency, reference, return_url="", metadata=None):
        """Server-create a Geidea session; the client completes it with the returned session id. The
        Basic-auth key never crosses to the browser."""
        from app.ports.payment_gateway import CheckoutSession
        body = {"amount": float(major(amount_minor, currency)), "currency": currency.upper(),
                "merchantReferenceId": reference}
        if return_url:
            body["returnUrl"] = return_url
        code, resp = post_json(f"{self._base}/pgw/api/v2/direct/session", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Geidea unreachable")
        if self._code(resp) != _OK:
            return CheckoutSession(ok=False, provider="geidea",
                                   error=str(resp.get("responseMessage", self._code(resp))))
        session = resp.get("session") or {}
        return CheckoutSession(ok=True, provider="geidea", id=str(session.get("id", "")),
                               widget={"kind": "geidea", "session_id": str(session.get("id", ""))},
                               expires_at=str(session.get("expiryDate", "")))

    def verify_webhook(self, *, headers, body, secret):
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:  # noqa: BLE001 — a non-JSON body is not a verified event
            evt = {}
        order = evt.get("order") or {}
        # Geidea does NOT sign the raw body. The callback carries a `signature` FIELD that is
        # HMAC-SHA256 (base64) keyed by the API PASSWORD over the concatenation
        #   {merchantPublicKey}{amount:.2f}{currency}{orderId}{status}{merchantReferenceId}{timestamp}
        # (verified against docs.geidea.net). Reconstruct it from the payload's own fields; the old
        # raw-body HMAC never matched Geidea's scheme, so no real callback ever verified.
        amount = order.get("amount", order.get("totalAmount", evt.get("amount", 0)))
        try:
            amount_str = f"{float(amount):.2f}"
        except (TypeError, ValueError):
            amount_str = str(amount)
        digest = "".join([
            self._pubkey, amount_str,
            str(order.get("currency", evt.get("currency", ""))),
            str(order.get("orderId", "")),
            str(order.get("status", "")),
            str(order.get("merchantReferenceId", "")),
            str(evt.get("timestamp", order.get("timestamp", ""))),
        ])
        expected = base64.b64encode(
            hmac.new(self._apipass.encode(), digest.encode(), hashlib.sha256).digest()).decode()
        # The signature is a body field; a header spelling is accepted as a fallback.
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        provided = str(evt.get("signature", "")) or h.get("x-geidea-signature", "") or h.get("signature", "")
        verified = constant_eq(provided, expected)
        status = str(order.get("status", evt.get("responseCode", ""))).lower()
        kind = "payment.succeeded" if status in ("success", "paid", _OK) else \
            "refund" if "refund" in status else "payment.failed"
        return WebhookEvent(verified=verified, type=kind, provider_id=str(order.get("orderId", "")))
