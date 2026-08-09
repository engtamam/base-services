"""Tap (KSA/GCC) — verified against developers.tap.company.

  charge   POST /v2/charges, Bearer sk, JSON, amount in MAJOR units (decimal), source {id: token}.
  refund   POST /v2/refunds, charge_id + amount (major).
  webhook  the `hashstring` header is HMAC-SHA256 (hex) over a FIXED FIELD DIGEST, NOT the raw body:
             x_id{id}x_amount{amount}x_currency{currency}x_gateway_reference{reference.gateway}
             x_payment_reference{reference.payment}x_status{status}x_created{transaction.created}
           amount is formatted to the currency's decimals ("1.00"); signed with the secret API key.
"""
from __future__ import annotations

from app.adapters.billing_http import bearer, post_json
from app.adapters.payment._common import constant_eq, hmac_sha256_hex, major
from app.core.currency import decimals as _decimals
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = {"CAPTURED", "AUTHORIZED"}


class TapGateway:
    driver = "tap"

    def __init__(self, api_key: str, base: str = "", webhook_secret: str = "", **_) -> None:
        if not api_key:
            raise GatewayUnavailable("PAYMENT_API_KEY (Tap secret key) required")
        self._h = {"Authorization": bearer(api_key)}
        self._base = (base or "https://api.tap.company").rstrip("/")
        self._whsec = webhook_secret

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        body = {"amount": float(major(amount_minor, currency)), "currency": currency.upper(),
                "description": description, "reference": {"transaction": idempotency_key},
                "source": {"id": token}, "metadata": metadata or {}}
        code, resp = post_json(f"{self._base}/v2/charges", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Tap unreachable")
        status = str(resp.get("status", "FAILED")).upper()
        return Charge(ok=status in _OK, status="succeeded" if status in _OK else "failed",
                      provider="tap", provider_id=str(resp.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if status in _OK else str((resp.get("response") or {}).get("message", status)))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        body = {"charge_id": provider_id, "reason": "requested_by_customer"}
        if amount_minor is not None:
            body["amount"] = float(major(amount_minor, currency))
        code, resp = post_json(f"{self._base}/v2/refunds", body, self._h)
        if code == 0:
            raise GatewayUnavailable("Tap unreachable")
        ok = str(resp.get("status", "")).upper() in ("REFUNDED", "PENDING")
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="tap",
                      provider_id=str(resp.get("id", provider_id)))

    def verify_webhook(self, *, headers, body, secret):
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:  # noqa: BLE001 — a non-JSON body is not a verified event
            evt = {}
        # Reconstruct Tap's field digest from the SAME payload fields and HMAC it — verifying the raw
        # body (the old behaviour) never matched Tap's header, so every webhook read as unverified.
        ref = evt.get("reference") or {}
        txn = evt.get("transaction") or {}
        currency = str(evt.get("currency", "") or "")
        try:
            amount = f"{float(evt.get('amount', 0)):.{_decimals(currency)}f}"
        except (TypeError, ValueError):
            amount = str(evt.get("amount", ""))
        digest = (f"x_id{evt.get('id','')}x_amount{amount}x_currency{currency}"
                  f"x_gateway_reference{ref.get('gateway','')}"
                  f"x_payment_reference{ref.get('payment','')}"
                  f"x_status{evt.get('status','')}x_created{txn.get('created','')}")
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        expected = hmac_sha256_hex(secret or self._whsec, digest.encode())
        verified = constant_eq(h.get("hashstring", ""), expected)
        status = str(evt.get("status", "")).upper()
        kind = "payment.succeeded" if status in _OK else "refund" if status == "REFUNDED" \
            else "payment.failed"
        return WebhookEvent(verified=verified, type=kind, provider_id=str(evt.get("id", "")))
