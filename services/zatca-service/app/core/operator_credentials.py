"""The signer credential, changeable by an operator after launch.

A tax-authority integration is needed after launch by definition: the credentials are issued by
ZATCA to the taxpayer, not baked into a deployment, and they are replaced on the authority's
schedule rather than on ours. Requiring an engineer and a redeploy to enter one does not stop that
from happening — it means it happens through whoever has shell access, at whatever hour.

WHAT IS DELIBERATELY NOT HERE. `ZATCA_SEAL_KEY` seals every stored CSID at rest and stays in the
environment, for the same reason `SECRETS_KEK` does: a key that protects the certificates and is
itself editable in the panel protects nothing from someone who has the panel, and rotating it at
runtime would make every stored certificate unreadable at once rather than one at a time.

Per-taxpayer CSIDs are already runtime data — they arrive through onboarding and are stored sealed.
This module is only about the credential this service uses to reach its signer.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

SIGNER_TOKEN = "ZATCA_SIGNER_TOKEN"


class SignerCredentials:
    def __init__(self, settings, secret_store, *, ttl_s: int = 30, now=time.monotonic) -> None:
        self._s = settings
        self._secrets = secret_store        # None when no KEK is configured
        self._ttl = ttl_s
        self._now = now
        self._cache: dict[str, tuple[float, str]] = {}

    async def token(self, tenant: str = "") -> str:
        """The signer token for this tenant: stored if set, else the configured one.

        Falling back to config means a deployment that has not migrated behaves exactly as it does
        today, so nothing has to be moved before this ships.
        """
        hit = self._cache.get(tenant)
        now = self._now()
        if hit and now < hit[0]:
            return hit[1]
        fallback = getattr(self._s, SIGNER_TOKEN, "") or ""
        value = fallback
        if self._secrets is not None:
            try:
                value = await self._secrets.get(tenant=tenant, name=SIGNER_TOKEN,
                                                fallback=fallback)
            except Exception:  # noqa: BLE001 — an unreadable row must not stop every clearance
                # Loud but not fatal. The configured token is a working answer, and a broken
                # credential is an operator problem rather than an invoicing outage — which for a
                # tax integration means a filing deadline, not just a failed request.
                _log.warning("signer_credential_unreadable")
        self._cache[tenant] = (now + self._ttl, value)
        return value

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop the cache so a panel change is visible on the next clearance."""
        if tenant is None:
            self._cache.clear()
        else:
            self._cache.pop(tenant, None)
