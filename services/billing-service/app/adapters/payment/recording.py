"""Recording gateway — pilot-safe. Records the intent, moves NO money (settled=False).

Use during onboarding/pilot: the flow runs end-to-end but nothing is charged. Honest —
it never mints a settlement, so invoices stay 'open' until a real gateway is wired.
"""
from __future__ import annotations

from app.ports.payment_gateway import Charge, WebhookEvent


class RecordingGateway:
    driver = "recording"

    def charge(self, *, amount_minor, currency, token, idempotency_key, description="", metadata=None):
        return Charge(ok=False, status="pending", provider="recording",
                      provider_id="rec_" + idempotency_key[:12], amount_minor=amount_minor,
                      currency=currency, error="recording mode — no live settlement")

    def refund(self, *, provider_id, amount_minor=None, currency="SAR"):
        return Charge(ok=False, status="pending", provider="recording", provider_id=provider_id)

    def verify_webhook(self, *, headers, body, secret):
        return WebhookEvent(verified=False, type="unknown")
