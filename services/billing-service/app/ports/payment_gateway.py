"""PaymentGateway port — charge / refund / verify_webhook across providers.

Adapters take a provider TOKEN/source (never a raw card — PCI SAQ-A). charge/refund
return a normalized Charge; a provider decline is a real answer (not an exception).
Only a genuinely unreachable/misconfigured gateway raises GatewayUnavailable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError


class GatewayUnavailable(PortError):
    """Gateway unreachable/unconfigured — retryable (dunning)."""


@dataclass
class Charge:
    ok: bool
    status: str                          # succeeded | failed | pending | refunded
    provider: str = ""
    provider_id: str = ""                # the gateway's charge/payment id
    amount_minor: int = 0
    currency: str = ""
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "status": self.status, "provider": self.provider,
                "provider_id": self.provider_id, "amount_minor": self.amount_minor,
                "currency": self.currency, "error": self.error}


@dataclass
class WebhookEvent:
    verified: bool
    type: str = ""                        # payment.succeeded | payment.failed | refund | unknown
    provider_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentGateway(Protocol):
    driver: str

    def charge(self, *, amount_minor: int, currency: str, token: str,
               idempotency_key: str, description: str = "", metadata: dict | None = None) -> Charge:
        ...

    def refund(self, *, provider_id: str, amount_minor: int | None = None,
               currency: str = "SAR") -> Charge:
        # `currency` is REQUIRED for a partial refund: the amount is formatted to the currency's real
        # precision (KWD/BHD/OMR = 3-dp), and a wrong exponent refunds 10x the intended amount. A
        # full refund (amount_minor=None) does not use it.
        ...

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, secret: str) -> WebhookEvent:
        ...
