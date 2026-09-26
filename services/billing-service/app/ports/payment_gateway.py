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
    provider_id: str = ""                 # the gateway's charge/payment id
    # The gateway's OWN unique id for THIS notification (Stripe evt_…, Checkout event id, …), when it
    # sends one. It is the strongest dedupe key: it distinguishes two genuinely-different events about
    # the same charge (e.g. two partial refunds). Empty ⇒ the handler falls back to a composite key
    # (provider:provider_id:type). Populated per adapter where the payload carries an event id.
    event_id: str = ""
    # For a `refund` event: the amount the gateway reports as refunded, in MINOR units. 0 = the gateway
    # did not report an amount (a bare "refunded" signal) → the handler treats it as a FULL refund of
    # the remainder. A POSITIVE value drives an amount-based PARTIAL refund: only that much is reversed
    # and the invoice stays paid until the total is reached. Each adapter populates it from its own
    # signed refund payload (see per-gateway extraction), converted to minor units for the currency.
    refunded_minor: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CheckoutSession:
    """A server-created checkout/session the CUSTOMER's widget or redirect completes — so the app
    never proxies a provider key. `ok=False` with a reason for a provider that has no server-side
    session (plug-and-play: it SAYS so, it does not fake one)."""
    ok: bool
    provider: str = ""
    id: str = ""                          # the provider's checkout/session id
    redirect_url: str = ""                # redirect-flow providers
    widget: dict[str, Any] = field(default_factory=dict)   # config the client widget needs (no key)
    expires_at: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "provider": self.provider, "id": self.id,
                "redirect_url": self.redirect_url, "widget": self.widget,
                "expires_at": self.expires_at, "error": self.error}


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

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        # OPTIONAL: only providers with a server-side checkout/session implement this. The route
        # checks for it and returns 'unsupported' otherwise — a provider that cannot must not fake it.
        ...

    def fetch_status(self, *, provider_id: str, currency: str = "") -> Charge:
        # OPTIONAL: pull the CURRENT status of a charge/payment straight from the provider, for
        # RECONCILIATION when a webhook was missed or failed verification. Returns a Charge whose
        # `status` is succeeded | failed | refunded | pending. Only providers with a status GET
        # implement it; the reconcile path reports 'unsupported' otherwise — it never fakes a status,
        # because inventing "paid" is the one answer a billing reconcile must never give.
        ...
