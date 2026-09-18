"""PaymentGateway port — the conduit's ONLY contract with a shop's own payment gateway.

payments-service is a CONDUIT, never a PSP: it uses each shop's OWN gateway credentials, SERVER-SIDE
ONLY, to mint a hosted checkout link per order; funds flow customer → the shop's gateway → the shop,
and never through this platform. So the port is deliberately small — three operations, no card data
ever touches it (PCI stays on the gateway's hosted page):

  * prepare_checkout — create a hosted payment link for a server-authoritative amount. Returns a
    CheckoutSession {redirect_url, provider_ref}. A provider with no server-side session says so
    (ok=False + reason) rather than faking one.
  * verify_webhook   — turn a gateway's status POST into a trustworthy PaymentOutcome. An UNSIGNED
    gateway (e.g. TuwaiqPay) must verify-by-fetch (re-query the gateway) rather than trust the body.
  * fetch_status     — pull the current status straight from the gateway, for RECONCILIATION when a
    webhook is missed or unverifiable. It never invents "paid".

Adapters are constructed PER TENANT from that tenant's sealed MERCHANT_* credentials (see
core/merchant_credentials); the instance is short-lived and holds the plaintext only in memory.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from app.ports.errors import PortError

# Normalised payment statuses. "succeeded" is the only one that means money moved.
SUCCEEDED, FAILED, CANCELLED, PENDING = "succeeded", "failed", "cancelled", "pending"


class GatewayUnavailable(PortError):
    """The shop's gateway is unreachable or misconfigured — retryable (reconcile later). Distinct
    from a gateway that answers with a decline, which is a real PaymentOutcome, not an exception."""


@dataclass
class CheckoutSession:
    """A hosted checkout the CUSTOMER completes on the gateway's own page — so no gateway key or card
    data ever passes through us. ok=False (with a reason) for a gateway that cannot mint a link."""

    ok: bool
    provider: str = ""
    provider_ref: str = ""          # the gateway's own id for this checkout (fetch_status/webhook key)
    redirect_url: str = ""          # the hosted link to send the customer to
    expires_at: str = ""            # the gateway's stated expiry, if any (informational)
    extra: dict[str, Any] = field(default_factory=dict)   # provider ids to persist (never secrets)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "provider": self.provider, "provider_ref": self.provider_ref,
                "redirect_url": self.redirect_url, "expires_at": self.expires_at,
                "extra": self.extra, "error": self.error}


@dataclass
class PaymentOutcome:
    """What the gateway says HAPPENED to a payment — from a verified webhook or a status fetch."""

    status: str = PENDING           # succeeded | failed | cancelled | pending
    provider: str = ""
    provider_ref: str = ""          # the gateway's checkout/transaction id
    amount_minor: int = 0           # what the gateway reports was charged (verify vs our expected)
    currency: str = ""
    event_id: str = ""              # the gateway's unique id for THIS notification, when it sends one
    verified: bool = False          # did WE confirm it (signature or verify-by-fetch)?
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentGateway(Protocol):
    driver: str

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        ...

    def verify_webhook(self, *, headers: dict[str, str], body: bytes) -> PaymentOutcome:
        ...

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        ...
