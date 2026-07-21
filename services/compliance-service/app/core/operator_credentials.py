"""The credentials this service uses to reach somebody else, resolvable after launch.

Two of them, and both are needed after launch by definition. A third party's erasure token is
issued by that third party on their schedule, not ours; the job-service token is rotated by whoever
runs job-service. Requiring an engineer and a redeploy to enter one does not stop that happening —
it means it happens through whoever has shell access, at whatever hour.

CONFIG AND THE SECRETS PORT REMAIN THE FALLBACK. A deployment with nothing stored resolves exactly
what it resolves today — the secrets port for a target token, the environment for the job token —
so nothing has to be migrated first and a service booted without a KEK never notices this exists.

THE CACHE IS WHY THIS IS AFFORDABLE. A sweep drives every unacked target of every open DSAR;
resolving per attempt would cost a datastore read and an AES open each time, to support a change
made a few times a year. The trade is that a rotation lands within the refresh window rather than
instantly — survivable only because the previous credential stays valid through the store's
rotation window, and `assert_operator_config_ok` refuses a refresh longer than that window so the
two always overlap.

AN UNREADABLE STORED CREDENTIAL IS NOT FATAL. If the KEK no longer matches what sealed it, the
configured value is still a working answer, and a broken row is an operator problem rather than a
DSAR that misses its statutory deadline. The failure is already loud in the operator surface, which
is where it gets fixed.

WHAT IS DELIBERATELY NOT HERE. `VAULT_TOKEN` is how the secrets port reads everything else and
`SECRETS_KEK` seals every row this module reads — both are bootstrap material and stay in the
environment, for the same reason. `INTERNAL_SERVICE_TOKEN` and `PII_SALT` are deploy-time identity
of the deployment itself; the salt is frozen besides.
"""
from __future__ import annotations

import logging
import time

_log = logging.getLogger(__name__)

JOB_SERVICE_TOKEN = "JOB_SERVICE_TOKEN"


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
            _log.warning("operator_credential_unreadable", extra={"credential": name})
        self._cache[key] = (now + self._ttl, value)
        return value

    def invalidate(self, tenant: str | None = None) -> None:
        """Drop this replica's copy so a panel change is visible on the next dispatch.

        Only this replica: another process cannot be reached from here, which is what the refresh
        window is still for. This makes the common case — set a token, immediately re-drive a
        stuck DSAR — instant rather than up to a window late.
        """
        if tenant is None:
            self._cache.clear()
        else:
            for key in [k for k in self._cache if k[0] == tenant]:
                self._cache.pop(key, None)
