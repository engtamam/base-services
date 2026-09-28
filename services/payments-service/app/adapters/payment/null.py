"""Null gateway — fail-closed. A tenant that has NOT configured a gateway resolves to this, and it
refuses every operation with a clear reason rather than silently doing nothing (which would look like
a payment that never arrives). It never fakes a link or an outcome."""
from __future__ import annotations

from app.ports.payment_gateway import PENDING, CheckoutSession, GatewayUnavailable, PaymentOutcome

_MSG = ("no payment gateway is configured for this tenant — set the shop's MERCHANT_* credentials "
        "first (write-only, sealed)")


class NullGateway:
    driver = "null"

    def __init__(self, **_) -> None:
        pass

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        return CheckoutSession(ok=False, provider=self.driver, error=_MSG)

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, stored_currency: str = "") -> PaymentOutcome:
        return PaymentOutcome(status=PENDING, provider=self.driver, verified=False)

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        raise GatewayUnavailable(_MSG)
