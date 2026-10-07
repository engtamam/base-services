"""MARKETPLACE_* credentials — the PLATFORM's own marketplace-provider secrets, sealed and platform-scoped.

The marketplace model is the mirror image of the conduit's `MERCHANT_*` store:
  * conduit      → each SHOP's own gateway key, strictly PER-TENANT (merchant_credentials).
  * marketplace  → the PLATFORM's single marketplace account key, PLATFORM-SCOPED (here).

Both are a "key to money", so this store gets the SAME highest-isolation treatment as the merchant store,
in its OWN sealed collection under its OWN derived key (a distinct scrypt salt), so a leak of one
credential class can never open another and the two ciphertexts are not even under the same key:
  * it is WRITE-ONLY at the API boundary (a route may set/clear/hint, never read the plaintext);
  * the plaintext is read ONLY here, in-process, to construct a short-lived marketplace adapter;
  * it is PLATFORM-SCOPED (tenant=""): there is exactly one marketplace account for the whole platform,
    so — unlike the per-tenant merchant store — reads and writes use the empty tenant. A per-tenant
    override is neither offered nor inherited.

The credential NAMES a platform operator sets (via the write-only route):
  MARKETPLACE_PROVIDER        which marketplace adapter ("tap", "moyasar", …)
  MARKETPLACE_SECRET_KEY      the provider's platform marketplace secret/API key (Bearer)
  MARKETPLACE_API_BASE        provider base URL override (sandbox vs production), optional
  MARKETPLACE_WEBHOOK_SECRET  secret for verifying inbound payout/charge webhooks the provider signs
"""
from __future__ import annotations

from dataclasses import dataclass

from app.core.secret_store import SecretStore

MARKETPLACE_COLLECTION = "marketplace_secrets"
MARKETPLACE_SALT = b"marketplace-payment-secrets-v1"

PROVIDER = "MARKETPLACE_PROVIDER"
SECRET_KEY = "MARKETPLACE_SECRET_KEY"
API_BASE = "MARKETPLACE_API_BASE"
WEBHOOK_SECRET = "MARKETPLACE_WEBHOOK_SECRET"

# The names a write-only route may set/clear/hint. A name outside this set is refused, so a caller cannot
# smuggle arbitrary keys into the sealed store.
SETTABLE_NAMES = (PROVIDER, SECRET_KEY, API_BASE, WEBHOOK_SECRET)

# The platform scope — the marketplace account belongs to the platform, not any one tenant.
PLATFORM = ""


def marketplace_secret_store(store, kek: str, *, rotation_window_s: int = 300) -> SecretStore:
    """A SecretStore bound to the isolated marketplace collection + salt.

    platform_fallback=False: there is no per-tenant marketplace credential to fall back FROM — every
    value is platform-scoped (tenant="") and read as such — so the inheritance path is simply off.
    require_tenant stays False BECAUSE the whole store is platform-scoped: the one legitimate scope IS
    the empty tenant, so a "" write must be allowed here (the opposite of the merchant store's rule)."""
    return SecretStore(store, kek, rotation_window_s=rotation_window_s,
                       collection=MARKETPLACE_COLLECTION, salt=MARKETPLACE_SALT,
                       platform_fallback=False, require_tenant=False)


@dataclass(frozen=True)
class MarketplaceConfig:
    provider: str
    secret_key: str = ""
    base: str = ""
    webhook_secret: str = ""


class MarketplaceCredentials:
    """Resolve the platform's sealed marketplace credentials into a ready config. The plaintext lives
    only inside the returned config for one operation (the adapter is built from it per request)."""

    def __init__(self, secrets: SecretStore) -> None:
        self._secrets = secrets

    async def provider(self) -> str:
        """The platform's selected marketplace provider/driver, or "" if none is configured."""
        return (await self._secrets.get(tenant=PLATFORM, name=PROVIDER, fallback="")).strip().lower()

    async def configured(self) -> bool:
        """True only when BOTH a provider and its secret key are set — a provider with no key cannot
        authenticate, so it is 'not configured' (fail-closed), never a half-ready marketplace."""
        return bool(await self.provider()) and bool(
            (await self._secrets.get(tenant=PLATFORM, name=SECRET_KEY, fallback="")).strip())

    async def config(self) -> MarketplaceConfig:
        async def g(name: str) -> str:
            return await self._secrets.get(tenant=PLATFORM, name=name, fallback="")

        base = await g(API_BASE)
        # Re-validate the provider base ON READ, not only at write (SSRF / money-redirection): the
        # write-time guard is bypassable by a backup import / direct store write, so a non-https or
        # private/internal base could otherwise reach the fetch path. A stored base that fails validation
        # is dropped here, so the adapter falls back to the provider's own default base rather than
        # calling an attacker-chosen host. Mirrors MerchantGateways._config exactly.
        if base:
            from app.adapters.payment_http import validate_gateway_base
            ok, _reason = validate_gateway_base(base)
            if not ok:
                base = ""
        return MarketplaceConfig(
            provider=await self.provider(),
            secret_key=await g(SECRET_KEY), base=base, webhook_secret=await g(WEBHOOK_SECRET))

    async def webhook_secrets(self) -> tuple[str, ...]:
        """Every webhook secret currently valid (current + previous during rotation) — used where WE
        verify a signed inbound webhook and must not reject a not-yet-rotated provider secret."""
        return await self._secrets.accepted(tenant=PLATFORM, name=WEBHOOK_SECRET)
