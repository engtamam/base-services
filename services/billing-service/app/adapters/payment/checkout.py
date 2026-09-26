"""Checkout.com — verified: POST /payments, Bearer sk, JSON, amount in MINOR units,
source {type: token, token}. Webhook Cko-Signature = HMAC-SHA256 hex of the body."""
from __future__ import annotations

from app.adapters.billing_http import bearer, get_json, post_json
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

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        body = {} if amount_minor is None else {"amount": int(amount_minor)}
        code, resp = post_json(f"{self._base}/payments/{provider_id}/refunds", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Checkout.com unreachable")
        ok = 200 <= code < 300
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="checkout",
                      provider_id=str(resp.get("action_id", provider_id)))

    def fetch_status(self, *, provider_id, currency=""):
        """GET /payments/{id} — reconcile a missed webhook."""
        code, resp = get_json(f"{self._base}/payments/{provider_id}", self._h)
        if code == 0:
            raise GatewayUnavailable("Checkout.com unreachable")
        raw = str(resp.get("status", ""))
        status = ("succeeded" if raw in _OK else "refunded" if raw == "Refunded"
                  else "pending" if raw == "Pending" else "failed")
        return Charge(ok=status == "succeeded", status=status, provider="checkout",
                      provider_id=str(resp.get("id", provider_id)), currency=str(resp.get("currency", currency)))

    def verify_webhook(self, *, headers, body, secret):
        # Checkout.com HMAC-SHA256s the RAW body (hex) with the webhook signature key, in the
        # Cko-Signature header. Verify against the raw bytes — re-serialising the JSON would change
        # the precision of some values and the signature would no longer match.
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        verified = constant_eq(h.get("cko-signature", ""), hmac_sha256_hex(secret or self._whsec, body))
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:
            evt = {}
        t = str(evt.get("type", ""))
        kind = "payment.succeeded" if t in ("payment_approved", "payment_captured") else \
            "refund" if "refund" in t else "payment.failed" if "declined" in t or "failed" in t else "unknown"
        data = (evt.get("data") or {})
        # Refund amount (FIN-015): Checkout.com amounts are MINOR units; the payment_refunded event's
        # `data.amount` is this refund's amount. Verified 2026-09-26 against checkout.com docs.
        refunded = 0
        if kind == "refund":
            try:
                refunded = int(data.get("amount", 0) or 0)
            except Exception:  # noqa: BLE001
                refunded = 0
        return WebhookEvent(verified=verified, type=kind,
                            event_id=str(evt.get("id", "")),   # Checkout.com per-event id
                            provider_id=str(data.get("id", "")), refunded_minor=refunded)
