"""Null gateway — fails closed. No gateway configured => never fabricates a charge."""
from __future__ import annotations

from app.ports.payment_gateway import Charge, GatewayUnavailable, WebhookEvent

_MSG = "no payment gateway configured (set PAYMENT_GATEWAY_DRIVER); charge not attempted"


class NullGateway:
    driver = "null"

    def charge(self, **kwargs) -> Charge:
        raise GatewayUnavailable(_MSG)

    def refund(self, **kwargs) -> Charge:
        raise GatewayUnavailable(_MSG)

    def verify_webhook(self, **kwargs) -> WebhookEvent:
        return WebhookEvent(verified=False, type="unknown")
