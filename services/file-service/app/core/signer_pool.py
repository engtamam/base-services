"""The signed-URL secret, resolved at runtime instead of captured at boot.

SIGNED_URL_SECRET is the one credential this service owns. It is not a provider's key — it is OUR
signing key for share links — which makes rotating it a security operation an operator must be able
to perform, and today it needs a redeploy because the container turns it into an `HmacSigner` once
at startup and never reads it again.

IT IS ON THE HOT PATH. Every upload response, every share link and every public signed download
touches the signer, so resolving the credential per request would put a datastore read and an
AES-GCM open in front of traffic that is otherwise a single object-store call. The resolved signer
is therefore cached for `SIGNER_REFRESH_SECONDS`, and the operator surface invalidates this
replica's cache on a write so the common case — rotate the key, immediately test a link — is
instant rather than up to a window late.

THE CONFIGURED SIGNER IS THE DEFAULT, and it is REUSED rather than rebuilt when nothing was stored.
A deployment that has not migrated resolves to exactly the object the container built, so its
behaviour is unchanged and there is nothing to move first.

ROTATION IS NOT INSTANT, DELIBERATELY. The previous secret is carried into the signer as an
additional VERIFY key for as long as the store's rotation window keeps it, because a share link
already in somebody's inbox cannot be recalled and must not turn into a 403 the moment the key
changes. Signing only ever uses the current secret.
"""
from __future__ import annotations

import time

from app.core.secret_store import SecretError

# The setting an operator replaces. Named exactly as the environment variable so the panel's hint
# ("which key is loaded") lines up with what an engineer would grep for.
SIGNED_URL_SECRET = "SIGNED_URL_SECRET"


class SignerPool:
    def __init__(self, *, secret_store, configured, build,
                 refresh_seconds: int, now=time.monotonic) -> None:
        self._store = secret_store
        self._configured = configured          # what the container built from the environment
        self._build = build                    # (secret, also_accept) -> signer
        self._refresh = refresh_seconds
        self._now = now
        self._cached = None
        self._until = 0.0

    def invalidate(self) -> None:
        """Drop this replica's resolved signer. Another replica's cache cannot be reached from
        here — the refresh window still bounds how stale that one may be."""
        self._cached = None
        self._until = 0.0

    async def get(self):
        """The signer to use for this request."""
        if self._cached is not None and self._now() < self._until:
            return self._cached
        signer = await self._resolve()
        self._cached = signer
        self._until = self._now() + self._refresh
        return signer

    async def _resolve(self):
        if self._store is None:
            return self._configured
        try:
            stored = await self._store.accepted(tenant="", name=SIGNED_URL_SECRET)
        except SecretError:
            # A credential sealed under a KEK we no longer hold must not take share links down.
            # The environment value is a working answer and is what this deployment used before
            # anything was stored; the decrypt failure is loud in the operator surface instead.
            return self._configured
        if not stored:
            return self._configured
        current, previous = stored[0], stored[1:]
        return self._build(current, previous)
