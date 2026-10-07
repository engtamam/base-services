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

# ── capability + model taxonomy (the extensibility contract) ──────────────────────
# This is the vocabulary a multi-mode, multi-provider payments service is built on. It lives in the
# port (not an adapter) so the core reasons about WHAT a gateway can do without importing any concrete
# adapter — exactly as `driver` already lets the core name an adapter without knowing its class.
#
# A payment MODEL is how a payment is structured end to end:
MODEL_CONDUIT = "conduit"          # the shop's OWN gateway; funds flow customer -> gateway -> shop (today)
MODEL_MARKETPLACE = "marketplace"  # the platform's account; the provider splits + pays out to sub-merchants
MODELS = frozenset({MODEL_CONDUIT, MODEL_MARKETPLACE})

# A CAPABILITY is one operation an adapter can perform. An adapter DECLARES the set it supports, and the
# core asserts the capability it needs BEFORE using it (fail-closed), so an unsupported op is a clear
# refusal, never a half-done call.
CAP_CHECKOUT = "checkout"          # mint a hosted pay-link (every gateway today)
CAP_ONBOARDING = "onboarding"      # register a sub-merchant with the provider (marketplace)
CAP_SPLIT = "split"                # split one payment across destinations + a platform fee (marketplace)
CAP_PAYOUT = "payout"              # report/settle a sub-merchant payout (marketplace)
CAPABILITIES = frozenset({CAP_CHECKOUT, CAP_ONBOARDING, CAP_SPLIT, CAP_PAYOUT})

# An ONBOARDING MODE is how a merchant's KYB data reaches the provider (only meaningful for marketplace):
ONBOARD_API = "api"                # we collect in our UI and forward via the provider API (mode A)
ONBOARD_HOSTED = "hosted"          # the merchant submits KYC DIRECTLY to the provider (mode B, e.g. JS Connect)
ONBOARDING_MODES = frozenset({ONBOARD_API, ONBOARD_HOSTED})


def _as_str_set(val) -> frozenset[str]:
    """Coerce a DECLARED metadata value into a clean set of strings, defensively and FAIL-CLOSED.

    The declaration is trusted code, but a bug or a hand-rolled/misconfigured adapter must never widen
    what the core will do:
      * None / empty            -> frozenset()  (declares nothing; the core refuses the op it wanted)
      * a bare str "checkout"   -> {"checkout"}  (NOT split into characters — the common mistake, handled)
      * a set/list/tuple        -> its string elements
      * anything non-iterable   -> frozenset()  (never raise on a capability lookup)
    The one direction this never does is INVENT a capability, so a malformed declaration can only ever
    make the core refuse more, never permit more."""
    if not val:
        return frozenset()
    if isinstance(val, str):
        return frozenset({val})
    try:
        return frozenset(str(x) for x in val)
    except TypeError:
        return frozenset()


def capabilities_of(gateway) -> frozenset[str]:
    """The capabilities an adapter DECLARES. getattr-with-default + defensive coercion so a gateway that
    predates the declaration (or the fail-closed null, or a malformed declaration) simply advertises
    nothing valid, rather than raising — the core then refuses the op it wanted, the safe direction."""
    return _as_str_set(getattr(gateway, "capabilities", ()))


def model_of(gateway) -> str:
    """The payment model an adapter DECLARES ("" if it declares none or declares a non-string)."""
    val = getattr(gateway, "model", "")
    return val if isinstance(val, str) else ""


def onboarding_modes_of(gateway) -> frozenset[str]:
    """The onboarding modes an adapter DECLARES (empty for a conduit gateway, which has none)."""
    return _as_str_set(getattr(gateway, "onboarding_modes", ()))


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
    # The extensibility contract: every adapter declares its payment model + the capability set it
    # supports (and, for a marketplace adapter, its onboarding modes). The core reads these to pick and
    # assert behaviour without importing the concrete adapter. A conduit gateway declares
    # model=MODEL_CONDUIT, capabilities={CAP_CHECKOUT}, onboarding_modes=frozenset().
    model: str
    capabilities: frozenset[str]
    onboarding_modes: frozenset[str]

    def prepare_checkout(self, *, amount_minor: int, currency: str, reference: str,
                         return_url: str = "", metadata: dict | None = None) -> CheckoutSession:
        ...

    def verify_webhook(self, *, headers: dict[str, str], body: bytes, stored_currency: str = "") -> PaymentOutcome:
        ...

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        ...


# ── marketplace model: split checkout, sub-merchant onboarding, payout ─────────────
# Payout (settlement) statuses — normalised, like the charge statuses above.
PAYOUT_PENDING, PAYOUT_PAID, PAYOUT_FAILED = "pending", "paid", "failed"


@dataclass
class SplitDestination:
    """One sub-merchant's share of a split payment: the provider's destination id + the amount (in minor
    units) to route to it. The platform's commission is the IMPLICIT remainder (total - sum of shares),
    which stays with the platform's master account — never an attacker-settable field."""

    destination_id: str
    amount_minor: int

    def to_dict(self) -> dict[str, Any]:
        return {"destination_id": self.destination_id, "amount_minor": int(self.amount_minor)}


@dataclass
class OnboardResult:
    """The outcome of registering a sub-merchant with the provider. `destination_id` is the ONLY durable
    thing we persist (never the IBAN/documents). For hosted onboarding (mode B) the merchant finishes on
    the provider, so `redirect_url`/`handle` carries the embed/link and `status` is pending until a
    callback confirms. ok=False (+ error) when the provider refuses."""

    ok: bool
    provider: str = ""
    destination_id: str = ""
    status: str = "pending"          # pending | active | rejected
    redirect_url: str = ""           # hosted (mode B) embed/continue URL, when applicable
    handle: str = ""                 # hosted SDK handle/token, when applicable (never a secret)
    extra: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "provider": self.provider, "destination_id": self.destination_id,
                "status": self.status, "redirect_url": self.redirect_url, "handle": self.handle,
                "extra": self.extra, "error": self.error}


@dataclass
class PayoutStatus:
    """What the provider says about a sub-merchant payout/settlement — for reconciliation."""

    status: str = PAYOUT_PENDING     # pending | paid | failed
    provider: str = ""
    payout_ref: str = ""
    amount_minor: int = 0
    currency: str = ""
    verified: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class MarketplaceGateway(Protocol):
    """The contract for a MARKETPLACE provider (Tap, Moyasar …): the platform's single account splits one
    payment across sub-merchants and the provider pays them out. Distinct from the conduit PaymentGateway:
    a checkout carries `destinations`, and there are onboarding + payout operations. An adapter may
    implement BOTH protocols (a marketplace that also offers plain checkout). The core asserts the
    `split`/`onboarding`/`payout` capability before using the matching method (fail-closed)."""

    driver: str
    model: str
    capabilities: frozenset[str]
    onboarding_modes: frozenset[str]

    def prepare_split_checkout(self, *, amount_minor: int, currency: str, reference: str,
                               destinations: list[SplitDestination], return_url: str = "",
                               metadata: dict | None = None) -> CheckoutSession:
        ...

    def verify_webhook(self, *, headers: dict[str, str], body: bytes,
                       stored_currency: str = "") -> PaymentOutcome:
        ...

    def fetch_status(self, *, provider_ref: str, currency: str = "") -> PaymentOutcome:
        ...

    def onboard_merchant(self, *, mode: str, data: dict) -> OnboardResult:
        ...

    def fetch_payout_status(self, *, payout_ref: str) -> PayoutStatus:
        ...
