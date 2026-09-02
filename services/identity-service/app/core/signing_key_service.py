"""Runtime home of the signing keyring: load it, seal it at rest, cache it, rotate it on a cron.

The domain (`signing_keyring.Keyring`) is pure. This layer gives it the three things it deliberately
lacks — persistence, a clock, and a cache — while touching nothing about how keys are minted or
retired.

PERSISTENCE reuses `SecretStore`: the keyring is one sealed JSON row under a platform-wide name, so
the private keys sit encrypted at rest under the same `SECRETS_KEK` as every other durable secret,
and a stolen database yields ciphertext. Nothing new about key custody is invented here.

THE CACHE IS WHY SIGNING STAYS FAST. `issue_access` is on the login/refresh hot path and is
synchronous; it cannot await a database read per token. So the service keeps the ring in memory and
the issuer reads the current (kid, private_pem) straight from it. A background refresh re-reads the
sealed row every `refresh_seconds`, which is how a second identity replica — or the pod that did NOT
run the rotation — picks up a new key without a redeploy.

ROTATION IS IDEMPOTENT UNDER A REPEATED CRON. The job may fire twice, or a retry may land; rotating
on every call would mint keys endlessly and bloat the JWKS. So a rotation is a no-op when the current
key is younger than `min_age_seconds` (a fraction of the cadence), and the route reports which
happened. Last-writer-wins on the sealed row is acceptable because the guard makes concurrent
rotations collapse to one within the min-age window.
"""
from __future__ import annotations

import logging
import time

from app.core.signing_keyring import Keyring

_log = logging.getLogger(__name__)

# Platform-wide (tenant "") — one signing keyring for the whole deployment, like TOKEN_PRIVATE_KEY.
KEYRING_NAME = "token_signing_keyring"


class SigningKeyService:
    def __init__(self, secret_store, *, overlap_seconds: float, seed_pem: str = "",
                 refresh_seconds: float = 45.0, min_age_seconds: float = 3600.0,
                 now=time.time) -> None:
        if secret_store is None:
            raise ValueError("SigningKeyService requires a SecretStore (SECRETS_KEK must be set)")
        if overlap_seconds <= 0:
            raise ValueError("overlap_seconds must be positive")
        self._secrets = secret_store
        self._overlap = float(overlap_seconds)
        self._seed_pem = seed_pem
        self._refresh_seconds = float(refresh_seconds)
        self._min_age = float(min_age_seconds)
        self._now = now
        self._ring: Keyring | None = None

    @property
    def refresh_seconds(self) -> float:
        return self._refresh_seconds

    # ── lifecycle ────────────────────────────────────────────────────────────────
    async def load(self) -> None:
        """Read the sealed keyring, or seed it from TOKEN_PRIVATE_KEY on first boot. Prunes lapsed
        keys and persists the pruned ring so the JWKS never grows without bound across restarts."""
        raw = await self._secrets.get(tenant="", name=KEYRING_NAME, fallback="")
        if raw:
            ring = Keyring.from_json(raw)
            if ring.prune(self._now()):
                await self._persist(ring)
            self._ring = ring
            return
        if not self._seed_pem:
            # No stored ring and no seed PEM: rotation is configured but there is no key to sign
            # with. The issuer stays on its static path; this service simply has nothing to serve.
            raise ValueError(
                "signing-key rotation is enabled but there is no key to seed from — set "
                "TOKEN_PRIVATE_KEY (it is adopted as the first key and then rotated automatically)")
        ring = Keyring.seed(self._seed_pem, now=self._now())
        await self._persist(ring)
        self._ring = ring
        _log.info("signing keyring seeded from TOKEN_PRIVATE_KEY (kid=%s)", ring.current.kid)

    async def reload(self) -> None:
        """Re-read the sealed keyring row so another replica's rotation is picked up. Named `reload`,
        NOT `refresh`: this reloads a GLOBAL signing key, it does not refresh a tenant-scoped token, so
        it is outside the T4 'a refresh must re-authorize the member' rule (and the tenancy gate). Best-
        effort: a read failure keeps the last good ring rather than dropping signing."""
        try:
            raw = await self._secrets.get(tenant="", name=KEYRING_NAME, fallback="")
            if raw:
                self._ring = Keyring.from_json(raw)
        except Exception:  # noqa: BLE001 — never let a refresh error take down signing
            _log.warning("signing keyring refresh failed; keeping the cached ring", exc_info=True)

    # ── issuer/JWKS reads (SYNC — hot path) ─────────────────────────────────────────
    def current_signing_key(self) -> tuple[str, str]:
        """(kid, private_pem) for the current signer. Raises if the ring was never loaded."""
        if self._ring is None:
            raise RuntimeError("signing keyring not loaded")
        cur = self._ring.current
        return cur.kid, cur.private_pem

    def jwks(self) -> dict[str, list[dict[str, str]]]:
        if self._ring is None:
            return {"keys": []}
        return self._ring.jwks(now=self._now())

    # ── rotation ────────────────────────────────────────────────────────────────────
    async def rotate(self, *, force: bool = False) -> dict[str, object]:
        """Mint a new signing key unless the current one is younger than min_age (idempotent cron).

        Returns {rotated, kid, reason, key_count}. `force` overrides the min-age guard for an
        operator-driven emergency rotation (a suspected key compromise cannot wait for the cadence).
        """
        if self._ring is None:
            await self.load()
        assert self._ring is not None
        now = self._now()
        age = now - self._ring.current.created_at
        if not force and age < self._min_age:
            return {"rotated": False, "kid": self._ring.current.kid,
                    "reason": f"current key is {int(age)}s old (< min {int(self._min_age)}s)",
                    "key_count": len(self._ring.keys())}
        new = self._ring.rotate(now=now, overlap_seconds=self._overlap)
        await self._persist(self._ring)
        _log.info("signing key rotated: new kid=%s, ring now holds %d key(s)",
                  new.kid, len(self._ring.keys()))
        return {"rotated": True, "kid": new.kid, "reason": "forced" if force else "cadence",
                "key_count": len(self._ring.keys())}

    async def _persist(self, ring: Keyring) -> None:
        await self._secrets.set(tenant="", name=KEYRING_NAME, value=ring.to_json(),
                                actor_ref="signing-key-rotation")
