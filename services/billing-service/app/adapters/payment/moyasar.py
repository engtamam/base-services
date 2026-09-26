"""Moyasar (KSA) — verified against docs.moyasar.com.

  charge   POST /v1/payments, HTTP Basic (secret key : empty). amount is MINOR units (halalas),
           source[type]=token, source[token]=<token>. A token/card source REQUIRES a callback_url
           (the 3DS return target); metadata is an object of STRING key/values echoed back on the
           payment and its webhook; given_id makes the create idempotent — a retried POST with the
           same id returns the original payment instead of charging twice.
  refund   POST /v1/payments/{id}/refund, amount (minor) for a partial refund.
  webhook  Moyasar does not HMAC-sign; the payload carries a `secret_token` the merchant sets on the
           webhook, compared in constant time against the configured value. type is payment_paid /
           payment_failed / payment_refunded; data is the payment object.
"""
from __future__ import annotations

import uuid

from app.adapters.billing_http import basic_auth, get_json, post_form
from app.adapters.payment._common import constant_eq
from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_OK = {"paid", "authorized"}
# Fixed namespace so a given idempotency_key always maps to the SAME UUID — Moyasar's given_id must
# be a UUID, and a deterministic one is what makes a safe retry return the original payment.
_GIVEN_NS = uuid.UUID("f5b0e9a2-3c1d-4e6f-8a7b-9c0d1e2f3a4b")


class MoyasarGateway:
    driver = "moyasar"

    def __init__(self, api_key: str, base: str = "", webhook_secret: str = "",
                 return_url: str = "", **_) -> None:
        if not api_key:
            raise GatewayUnavailable("PAYMENT_API_KEY (Moyasar secret key) required")
        self._h = {"Authorization": basic_auth(api_key, "")}
        self._base = (base or "https://api.moyasar.com").rstrip("/")
        self._whsec = webhook_secret
        self._callback = return_url

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        fields = {"amount": int(amount_minor), "currency": currency.upper(),
                  "description": description, "source[type]": "token", "source[token]": token,
                  # A retried POST with the same UUID returns the original payment — no double charge.
                  "given_id": str(uuid.uuid5(_GIVEN_NS, idempotency_key))}
        if self._callback:
            fields["callback_url"] = self._callback         # required by Moyasar for a token source
        for k, v in (metadata or {}).items():
            fields[f"metadata[{k}]"] = str(v)               # Moyasar metadata values are strings
        code, resp = post_form(f"{self._base}/v1/payments", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable")
        status = str(resp.get("status", "failed"))
        return Charge(ok=status in _OK, status="succeeded" if status in _OK else "failed",
                      provider="moyasar", provider_id=str(resp.get("id", "")),
                      amount_minor=int(amount_minor), currency=currency,
                      error="" if status in _OK else str((resp.get("source") or {}).get("message", status)))

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        fields = {} if amount_minor is None else {"amount": int(amount_minor)}
        code, resp = post_form(f"{self._base}/v1/payments/{provider_id}/refund", fields, self._h)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable")
        ok = str(resp.get("status", "")) in ("refunded", "paid")
        return Charge(ok=ok, status="refunded" if ok else "failed", provider="moyasar",
                      provider_id=str(resp.get("id", provider_id)))

    def fetch_status(self, *, provider_id, currency=""):
        """GET /v1/payments/{id} — the current status, for reconciling a missed webhook."""
        code, resp = get_json(f"{self._base}/v1/payments/{provider_id}", self._h)
        if code == 0:
            raise GatewayUnavailable("Moyasar unreachable")
        raw = str(resp.get("status", "")).lower()
        status = ("refunded" if raw == "refunded" else "succeeded" if raw in _OK or raw == "captured"
                  else "failed" if raw in ("failed", "voided") else "pending")
        return Charge(ok=status == "succeeded", status=status, provider="moyasar",
                      provider_id=str(resp.get("id", provider_id)),
                      amount_minor=int(resp.get("amount", 0) or 0), currency=str(resp.get("currency", currency)))

    def verify_webhook(self, *, headers, body, secret):
        import json
        try:
            evt = json.loads(body.decode())
        except Exception:  # noqa: BLE001 — a body that is not JSON is not a verified event
            evt = {}
        verified = constant_eq(str(evt.get("secret_token", "")), secret or self._whsec)
        # Prefer the explicit event type (payment_paid / payment_failed / payment_refunded); fall
        # back to the payment's own status when an older payload omits it.
        etype = str(evt.get("type", ""))
        status = str((evt.get("data") or {}).get("status", ""))
        if "refund" in etype or "refund" in status:
            kind = "refund"
        elif "paid" in etype or status in _OK:
            kind = "payment.succeeded"
        else:
            kind = "payment.failed"
        # Refund amount (FIN-015): Moyasar amounts are MINOR units (halalas); the payment object carries
        # the CUMULATIVE `refunded` total — safe under the cumulative-floor. Verified 2026-09-26 against
        # docs.moyasar.com. 0 when absent → full-refund fallback.
        refunded = 0
        if kind == "refund":
            try:
                refunded = int((evt.get("data") or {}).get("refunded", 0) or 0)
            except Exception:  # noqa: BLE001
                refunded = 0
        return WebhookEvent(verified=verified, type=kind,
                            event_id=str(evt.get("id", "")),   # Moyasar top-level event id (≠ data.id)
                            provider_id=str((evt.get("data") or {}).get("id", "")),
                            refunded_minor=refunded)
