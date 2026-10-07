"""MERCHANT_* credentials — each shop's OWN payment-gateway secrets, sealed and per-tenant.

This is the highest-isolation store in the service. Every value is a shop's key to its OWN money,
so:
  * it lives in its OWN sealed collection ("merchant_secrets") under its OWN derived key (a distinct
    scrypt salt from any other secret store) — a leak of one credential class cannot open another;
  * it is WRITE-ONLY at the API boundary (a route may set/clear/hint, never read the plaintext);
  * the plaintext is read ONLY here, in-process, to construct a short-lived gateway adapter for one
    operation, and is NEVER logged, echoed, or placed in an event or a stored row;
  * there is no platform-wide fallback — a shop's gateway is the shop's, so a tenant with nothing set
    resolves to the fail-closed null gateway (a clear refusal), never to some default merchant.

The credential NAMES a shop sets (via the write-only route):
  MERCHANT_GATEWAY_DRIVER   which gateway ("tuwaiqpay", "console", …)
  MERCHANT_API_KEY          gateway username / api key (TuwaiqPay: the +9665… mobile or email)
  MERCHANT_EXTRA_SECRET     gateway password / secondary secret (TuwaiqPay: the password)
  MERCHANT_API_BASE         gateway base URL override (UAT vs Production), optional
  MERCHANT_WEBHOOK_SECRET   shared secret for verifying inbound webhooks, where the gateway signs them
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.adapters.payment import SELECTABLE_DRIVERS, build_gateway
from app.core.secret_store import SecretStore
from app.ports.payment_gateway import PaymentGateway


@runtime_checkable
class CredentialResolver(Protocol):
    """How a tenant's payment credentials become a ready gateway — the seam that lets the core stay
    agnostic about HOW creds are sourced for a given payment model.

    Today there is exactly ONE implementation, `MerchantGateways` (per-tenant sealed `MERCHANT_*`), and
    the core depends on this Protocol, not the class. When the marketplace model arrives, a SECOND
    implementation resolves platform-scoped marketplace creds + the tenant's sub-merchant id — WITHOUT
    weakening MerchantGateways' strict per-tenant isolation (require_tenant=True, no "" fallback). The
    conduit path is unchanged; this is a typing/abstraction seam, not a behaviour change."""

    async def gateway_for(self, tenant: str) -> PaymentGateway: ...

    async def driver_for(self, tenant: str) -> str: ...

    async def configured(self, tenant: str) -> bool: ...

MERCHANT_COLLECTION = "merchant_secrets"
MERCHANT_SALT = b"merchant-payment-secrets-v1"

DRIVER = "MERCHANT_GATEWAY_DRIVER"
API_KEY = "MERCHANT_API_KEY"
EXTRA_SECRET = "MERCHANT_EXTRA_SECRET"
API_BASE = "MERCHANT_API_BASE"
WEBHOOK_SECRET = "MERCHANT_WEBHOOK_SECRET"

# The names a write-only route may set/clear/hint. A name outside this set is refused, so a caller
# cannot smuggle arbitrary keys into the sealed store.
SETTABLE_NAMES = (DRIVER, API_KEY, EXTRA_SECRET, API_BASE, WEBHOOK_SECRET)


def merchant_secret_store(store, kek: str, *, rotation_window_s: int = 300) -> SecretStore:
    """A SecretStore bound to the isolated merchant collection + salt.

    platform_fallback=False and require_tenant=True make the "no platform-wide fallback" guarantee in
    this module's docstring TRUE in code, not just in prose: a credential resolves STRICTLY to its own
    (tenant, name) — no "*" inheritance on read — and a platform-wide ("") merchant row is refused at
    the write, so even a backup restore/remap has nothing for a stray fallback to pick up.
    """
    return SecretStore(store, kek, rotation_window_s=rotation_window_s,
                       collection=MERCHANT_COLLECTION, salt=MERCHANT_SALT,
                       platform_fallback=False, require_tenant=True)


@dataclass(frozen=True)
class GatewayConfig:
    driver: str
    api_key: str = ""
    extra_secret: str = ""
    base: str = ""
    webhook_secret: str = ""


class MerchantGateways:
    """Resolve a tenant's sealed credentials into a ready-to-use PaymentGateway. Constructed instances
    are short-lived (one request) and hold plaintext only in memory."""

    def __init__(self, secrets: SecretStore, *, console_link_base: str = "",
                 gateway_timeout: int = 30, allow_console: bool = False) -> None:
        self._secrets = secrets
        self._console_link_base = console_link_base
        self._timeout = gateway_timeout
        # The console gateway is a FAKE that marks payments paid with no real money movement — a
        # dev/zero-infra double, never a live processor. In production it must not be selectable, or a
        # mis-set tenant driver would silently "collect" money that never arrived. Off by default;
        # enabled only outside production (the wiring passes ENV != "production").
        self._selectable = SELECTABLE_DRIVERS if allow_console \
            else tuple(d for d in SELECTABLE_DRIVERS if d != "console")

    async def driver_for(self, tenant: str) -> str:
        """The tenant's selected gateway driver, or "" if none is configured."""
        return (await self._secrets.get(tenant=tenant, name=DRIVER, fallback="")).strip().lower()

    async def configured(self, tenant: str) -> bool:
        return await self.driver_for(tenant) in self._selectable

    async def _config(self, tenant: str) -> GatewayConfig:
        async def g(name: str) -> str:
            return await self._secrets.get(tenant=tenant, name=name, fallback="")
        base = await g(API_BASE)
        # payments-012: re-validate the gateway base ON READ, not only at write. The write-time guard is
        # bypassable by a backup import / direct store write, so a non-https or private/internal base
        # could reach the fetch path (SSRF / money-redirection). A stored base that fails validation is
        # dropped here — gateway_for then falls back to the driver's own default base rather than calling
        # an attacker-chosen host.
        if base:
            from app.adapters.payment_http import validate_gateway_base
            ok, _reason = validate_gateway_base(base)
            if not ok:
                base = ""
        return GatewayConfig(
            driver=await self.driver_for(tenant),
            api_key=await g(API_KEY), extra_secret=await g(EXTRA_SECRET),
            base=base, webhook_secret=await g(WEBHOOK_SECRET))

    async def gateway_for(self, tenant: str) -> PaymentGateway:
        """Build the tenant's gateway from its sealed creds. Unknown/unset driver → null (fail-closed).
        The plaintext creds live only inside the returned adapter instance for this one operation."""
        cfg = await self._config(tenant)
        if cfg.driver not in self._selectable:
            return build_gateway("null")
        return build_gateway(cfg.driver, {
            "api_key": cfg.api_key, "extra_secret": cfg.extra_secret, "base": cfg.base,
            "webhook_secret": cfg.webhook_secret, "link_base": self._console_link_base,
            "timeout": self._timeout})

    async def webhook_secrets(self, tenant: str) -> tuple[str, ...]:
        """Every webhook secret currently valid for the tenant (current + previous during rotation) —
        used where WE verify a signed inbound webhook and must not reject a not-yet-rotated caller."""
        return await self._secrets.accepted(tenant=tenant, name=WEBHOOK_SECRET)
