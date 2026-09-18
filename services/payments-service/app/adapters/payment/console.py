"""Console gateway — the zero-infra default and the test double.

It mints a deterministic fake hosted link (pointed at a caller-supplied base) so the whole conduit
— create request, deliver link, redirect, webhook, reconcile — is exercisable with no real gateway
and no real money. It NEVER reaches a network. A webhook is "verified" by a shared echo secret so
tests can drive both the success and failure branches deterministically; production uses a real
adapter (e.g. tuwaiqpay) whose verification is cryptographic or verify-by-fetch.
"""
from __future__ import annotations

import json

from app.ports.payment_gateway import (CANCELLED, FAILED, PENDING, SUCCEEDED, CheckoutSession,
                                        PaymentOutcome)


class ConsoleGateway:
    driver = "console"

    def __init__(self, *, link_base: str = "https://pay.example.test/checkout",
                 webhook_secret: str = "", **_) -> None:
        self._base = link_base.rstrip("/")
        self._secret = webhook_secret
        # In-memory ledger of what THIS instance handed out, so fetch_status can answer for a
        # reference in tests. Per-instance (short-lived), never shared — no cross-tenant state.
        self._issued: dict[str, dict] = {}

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        provider_ref = f"console_{reference}"
        self._issued[provider_ref] = {"amount_minor": int(amount_minor), "currency": currency}
        return CheckoutSession(
            ok=True, provider=self.driver, provider_ref=provider_ref,
            redirect_url=f"{self._base}/{provider_ref}",
            extra={"reference": reference})

    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> PaymentOutcome:
        try:
            payload = json.loads(body.decode("utf-8", "replace")) if body else {}
        except Exception:   # noqa: BLE001
            return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)
        # A shared echo secret stands in for a real signature: the caller proves it knows the secret,
        # exactly where a real adapter would check an HMAC. A wrong/absent secret is unverified.
        supplied = headers.get("x-console-secret", "") or payload.get("secret", "")
        verified = bool(self._secret) and supplied == self._secret
        status = str(payload.get("status", PENDING))
        if status not in (SUCCEEDED, FAILED, CANCELLED, PENDING):
            status = PENDING
        return PaymentOutcome(
            status=status, provider=self.driver,
            provider_ref=str(payload.get("provider_ref", "")),
            amount_minor=int(payload.get("amount_minor", 0) or 0),
            currency=str(payload.get("currency", "")),
            event_id=str(payload.get("event_id", "")),
            verified=verified and status in (SUCCEEDED, FAILED, CANCELLED),
            raw={"status": status})

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        rec = self._issued.get(provider_ref)
        # A console instance is short-lived, so an unknown ref (reconcile in a fresh instance) reads
        # as pending rather than inventing an outcome — the same fail-safe a real adapter honours.
        if rec is None:
            return PaymentOutcome(status=PENDING, provider=self.driver, provider_ref=provider_ref,
                                  verified=False)
        return PaymentOutcome(status=PENDING, provider=self.driver, provider_ref=provider_ref,
                              amount_minor=rec["amount_minor"], currency=rec["currency"],
                              verified=True)
