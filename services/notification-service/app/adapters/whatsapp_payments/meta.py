"""Meta Graph native-payment adapter — lookup + refund (build-ahead, KSA-dormant).

Verified against Meta's WhatsApp payments (payments-in/pg) reference, 2026-09-19:
  lookup  GET  /{phone_number_id}/payments/{configuration}/{reference_id}  -> {status, transactions[]}
  refund  POST /{phone_number_id}/payments_refund  {reference_id, payment_config_id, speed, amount}
"""
from __future__ import annotations

import json as _json

from app.adapters.notify_http import bearer, get_json, post_json
from app.ports.whatsapp_payments import PaymentLookup, RefundResult

_DEFAULT_BASE = "https://graph.facebook.com/v26.0"
# Meta payment statuses -> our normalised status.
_STATUS = {"captured": "captured", "pending": "pending", "failed": "failed", "canceled": "canceled",
           "cancelled": "canceled"}


class MetaWhatsAppPayments:
    driver = "meta"

    def __init__(self, access_token: str, phone_number_id: str, base_url: str = "") -> None:
        self._base = (base_url or _DEFAULT_BASE).rstrip("/")
        self._phone = phone_number_id
        self._headers = bearer(access_token)

    def get_payment_status(self, *, configuration: str, reference_id: str) -> PaymentLookup:
        code, body = get_json(
            f"{self._base}/{self._phone}/payments/{configuration}/{reference_id}", headers=self._headers)
        if code == 0:
            return PaymentLookup(ok=False, reference_id=reference_id, error="graph unreachable")
        try:
            data = _json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:   # noqa: BLE001
            return PaymentLookup(ok=False, reference_id=reference_id, error="bad response")
        if code >= 400:
            return PaymentLookup(ok=False, reference_id=reference_id,
                                 error=str((data.get("error") or {}).get("message", code)))
        # The lookup carries the payment's authoritative status (and a transactions[] array).
        status = str(data.get("payment_status") or data.get("status") or "unknown").lower()
        txns = data.get("transactions") or []
        amt = (txns[0].get("amount") if txns and isinstance(txns[0], dict) else {}) or {}
        return PaymentLookup(
            ok=True, reference_id=reference_id, status=_STATUS.get(status, status),
            amount_minor=int(amt.get("value", 0) or 0), offset=int(amt.get("offset", 100) or 100),
            currency=str(amt.get("currency", "")),
            transaction_id=str(txns[0].get("id", "")) if txns else "",
            verified=True, raw=data)

    def refund(self, *, reference_id: str, configuration: str, amount_minor: int, currency: str,
               offset: int = 100, speed: str = "normal") -> RefundResult:
        payload = {
            "reference_id": reference_id, "payment_config_id": configuration, "speed": speed,
            "amount": {"currency": currency, "value": str(int(amount_minor)), "offset": str(int(offset))},
        }
        code, body = post_json(f"{self._base}/{self._phone}/payments_refund", payload, self._headers)
        if code == 0:
            return RefundResult(ok=False, error="graph unreachable")
        try:
            data = _json.loads(body) if isinstance(body, str) else (body or {})
        except Exception:   # noqa: BLE001
            return RefundResult(ok=False, error="bad response")
        if code >= 400:
            return RefundResult(ok=False, error=str((data.get("error") or {}).get("message", code)))
        return RefundResult(ok=True, refund_id=str(data.get("id", "")), status=str(data.get("status", "")))
