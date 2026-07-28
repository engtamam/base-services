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
        """Decrypt-and-verify a HyperPay (OPP) webhook.

        HyperPay does not sign its webhooks — it ENCRYPTS them with AES-256-GCM: the body is hex
        ciphertext, the IV is in `X-Initialization-Vector` and the GCM auth tag in
        `X-Authentication-Tag` (both hex), decrypted with the configured webhook key (hex). The GCM
        auth tag IS the verification: a wrong key or a tampered body fails the tag, so `verified`
        is True only when the decryption succeeds. No key configured is `verified=False` with a
        reason — never a silent pass, which is the one outcome a payment webhook must not have.
        """
        key_hex = secret or self._whsec
        if not key_hex:
            return WebhookEvent(verified=False, type="unknown",
                                raw={"error": "no webhook key configured (PAYMENT_WEBHOOK_SECRET)"})
        h = {str(k).lower(): v for k, v in (headers or {}).items()}
        iv_hex = h.get("x-initialization-vector", "")
        tag_hex = h.get("x-authentication-tag", "")
        if not iv_hex or not tag_hex:
            return WebhookEvent(verified=False, type="unknown",
                                raw={"error": "missing X-Initialization-Vector / X-Authentication-Tag"})
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
            raw = body.decode("ascii") if isinstance(body, (bytes, bytearray)) else str(body)
            ciphertext = bytes.fromhex(raw.strip())
            tag = bytes.fromhex(tag_hex.strip())
            # AESGCM.decrypt expects ciphertext||tag and verifies the tag — InvalidTag on mismatch.
            plaintext = AESGCM(bytes.fromhex(key_hex.strip())).decrypt(
                bytes.fromhex(iv_hex.strip()), ciphertext + tag, None)
        except Exception as exc:  # noqa: BLE001 — bad hex, wrong key, or a tampered body: unverified
            return WebhookEvent(verified=False, type="unknown", raw={"error": type(exc).__name__})

        import json
        try:
            data = json.loads(plaintext.decode("utf-8"))
        except Exception:  # noqa: BLE001 — verified bytes that are not the JSON we expect
            return WebhookEvent(verified=False, type="unknown", raw={"error": "decrypted body is not JSON"})

        payload = data.get("payload") or {}
        code = str((payload.get("result") or {}).get("code", ""))
        ok, _ = self._result_ok({"result": {"code": code}})
        kind = "refund" if str(payload.get("paymentType", "")).upper() == "RF" else (
            "payment.succeeded" if ok else "payment.failed")
        return WebhookEvent(
            verified=True, type=kind, provider_id=str(payload.get("id", "")),
            # A small projection, not the whole card-bearing payload: enough to reconcile.
            raw={"result_code": code,
                 "merchant_transaction_id": str(payload.get("merchantTransactionId", ""))})
