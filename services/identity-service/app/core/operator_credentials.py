"""The credential identity uses to reach the capability authority, changeable after launch.

`AUTHZ_INTERNAL_TOKEN` authenticates US to authz-service. It is rotated by whoever runs authz, on
their schedule rather than ours, and requiring an engineer and a redeploy to enter the new one does
not stop that happening — it means it happens through whoever has shell access, at whatever hour,
against the one dependency that fails closed on every login.

WHAT IS DELIBERATELY NOT HERE, and this is the important part for THIS service.

  * `TOKEN_SIGNING_SECRET` and `TOKEN_PRIVATE_KEY` sign the tokens the whole platform trusts, and
    `TOKEN_HS256_SECRET` / `TOKEN_PUBLIC_KEY` verify them. Changing signing material under live
    sessions invalidates every token already issued: every person on the platform is logged out at
    once, and every service holding a token starts seeing forgeries. That is a deploy-time
    migration with a key-overlap plan, not a form field.
  * `SECRETS_KEK` seals every row this module reads and `VAULT_TOKEN` is how the secrets port reads
    everything else — bootstrap material, environment only.
  * `INTERNAL_SERVICE_TOKEN` and `PII_SALT` are the deployment's own identity; the salt is frozen
    besides, and re-mapping it orphans every pseudonym in the platform.
  * A TOTP secret is not here either. It is generated per person at enrolment and returned once, to
    the enrolling human — that is the enrolment protocol, not an operator credential.

CONFIG IS THE FALLBACK, NOT THE OTHER WAY AROUND. A deployment with nothing stored resolves the
environment value and behaves exactly as it does today, so nothing has to be migrated first and a
service booted without a KEK never notices this exists.

THE CACHE IS WHY THIS IS AFFORDABLE ON THE MINT PATH. The trade is that a rotation lands within the
refresh window rather than instantly — survivable only because the previous credential stays valid
through the store's rotation window, and `assert_operator_config_ok` refuses a refresh longer than
that window so the two always overlap.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

AUTHZ_INTERNAL_TOKEN = "AUTHZ_INTERNAL_TOKEN"
COMPLIANCE_INTERNAL_TOKEN = "COMPLIANCE_INTERNAL_TOKEN"


class OperatorCredentials:
    """The current value of a named credential: operator-stored if set, configured otherwise."""

    def __init__(self, secret_store, *, ttl_s: float = 30.0, now=time.monotonic) -> None:
        self._secrets = secret_store            # None when no KEK is configured
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[tuple[str, str], tuple[float, str]] = {}

    async def get(self, name: str, *, fallback: str = "", tenant: str = "") -> str:
        if self._secrets is None:
            return fallback
        key = (tenant, name)
        hit = self._cache.get(key)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1]
        value = fallback
        try:
            value = await self._secrets.get(tenant=tenant, name=name, fallback=fallback)
        except Exception:      # noqa: BLE001 — undecryptable, or the store is momentarily down
            # Loud but not fatal. The configured token is a working answer, and a broken row is an
            # operator problem rather than a platform-wide login outage.
            _log.warning("operator_credential_unreadable", extra={"credential": name})
        self._cache[key] = (now + self._ttl, value)
        return value

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop this replica's copy so a panel change is visible on the next mint.

        Only this replica: another process cannot be reached from here, which is what the refresh
        window is still for. This makes the common case — set a token, immediately test a login —
        instant rather than up to a window late.
        """
        if tenant is None:
            self._cache.clear()
        else:
            for key in [k for k in self._cache if k[0] == tenant]:
                self._cache.pop(key, None)
