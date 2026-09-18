"""Operator-managed credentials — set from the panel, sealed under a key that never leaves the env.

An operator has to run the platform after launch: rotate an SMS key, enter tax-authority
credentials, replace a payment secret. If that needs an engineer and a deployment, it does not stop
happening — it gets worked around, usually by handing somebody shell access, which is worse than
the thing the restriction was protecting.

So credentials become runtime. Exactly one thing does not.

THE KEK STAYS IN THE ENVIRONMENT. Secrets are sealed with AES-256-GCM under a key derived from
`SECRETS_KEK`, which lives in the secret store and is never settable through any API. Secrecy
cannot be bootstrapped from a form: if the key protecting the secrets were itself editable in the
panel, panel access would own everything. A stolen database therefore yields ciphertext; a stolen
database AND the environment yields the secrets, which is exactly today's exposure — so this adds a
layer rather than trading one away.

WRITE-ONLY IS THE POINT, and it is a property of the API rather than of this class. Adapters must
read plaintext — an SMS client cannot send with a masked string — so `get()` exists and is for
in-process use only. What must never exist is an HTTP route that returns it. `hint()` is what the
panel gets: enough to tell which key is loaded, useless to anyone who steals it.

ROTATION KEEPS THE PREVIOUS VALUE FOR A WINDOW. A request in flight when a key changes must not
fail, and an operator who pastes the wrong thing needs a way back. The old value is retained with
an expiry and dropped after it.
"""
from __future__ import annotations

import base64
import hashlib
import os
import time
from dataclasses import dataclass

from app.ports.errors import ConfigError

_COLL = "operator_secrets"
_PREFIX = "v1:gcm:"

# scrypt is deliberately expensive (~tens of ms + 16 MiB). A SecretStore is often built PER REQUEST
# (the merchant store is), so deriving on every construction turns an unauthenticated request into a
# CPU/memory-exhaustion lever. The derived key is a pure function of (kek, salt), so it is memoized
# process-wide here — the derivation happens once per distinct (kek, salt), not once per request.
_DERIVED_KEY_CACHE: dict[tuple[str, bytes], bytes] = {}


def _derive_key(kek: str, salt: bytes) -> bytes:
    cache_key = (kek, salt)
    key = _DERIVED_KEY_CACHE.get(cache_key)
    if key is None:
        key = hashlib.scrypt(kek.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=32)
        _DERIVED_KEY_CACHE[cache_key] = key
    return key

# How much of a credential the panel may see. Four characters identifies which key is loaded and
# is useless on its own — a longer hint starts being a meaningful fraction of a short token.
_HINT_CHARS = 4


class SecretError(ValueError):
    """A credential that cannot be stored or read safely."""


@dataclass(frozen=True)
class SecretInfo:
    """What the panel is allowed to know. Deliberately has no field for the value."""

    name: str
    hint: str                 # e.g. "••••3f2a"
    set_at: str = ""
    set_by: str = ""          # a pseudonym, never an identity
    rotating: bool = False    # a previous value is still accepted


class SecretStore:
    def __init__(self, store, kek: str, *, now=time.time,
                 rotation_window_s: int = 300,
                 collection: str = _COLL, salt: bytes = b"operator-secrets-v1") -> None:
        if not kek:
            # Refused rather than defaulted. A store that silently accepted an empty key would
            # write plaintext-equivalent rows while every check reported "encrypted".
            raise ConfigError(
                "SECRETS_KEK is required to store operator-managed credentials. It must come from "
                "the environment or a secret manager — never from the admin panel, because a key "
                "settable in the UI protects nothing from someone who has the UI.")
        # Memoized by (kek, salt) so a per-request SecretStore does not re-run scrypt every request
        # (an unauthenticated DoS lever otherwise — see _derive_key). `salt` (and `collection`) let a
        # separate, higher-isolation store (e.g. shops' MERCHANT_* payment credentials) live in its
        # OWN table under its OWN derived key — a leak of one credential class never touches the
        # other, and the two ciphertexts are not even under the same key.
        self._key = _derive_key(kek, salt)
        self._coll = collection
        self._s = store
        self._now = now
        self._window = rotation_window_s

    # ── crypto ────────────────────────────────────────────────────────────────
    def _seal(self, text: str) -> str:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        iv = os.urandom(12)
        return _PREFIX + base64.b64encode(
            iv + AESGCM(self._key).encrypt(iv, text.encode("utf-8"), None)).decode("ascii")

    def _open(self, sealed: str) -> str:
        if not sealed.startswith(_PREFIX):
            raise SecretError("stored credential is not in a recognised format")
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        blob = base64.b64decode(sealed[len(_PREFIX):])
        try:
            return AESGCM(self._key).decrypt(blob[:12], blob[12:], None).decode("utf-8")
        except Exception as exc:   # noqa: BLE001 — wrong key, tampered blob: one condition here
            # Loud. Returning "" would make every provider call fail with "unauthorised" and send
            # somebody to the provider's dashboard instead of to the KEK they just changed.
            raise SecretError(
                "stored credential could not be decrypted — the KEK does not match the one it was "
                "sealed with. Restore the previous SECRETS_KEK, or re-enter the credentials.") from exc

    @staticmethod
    def _hint(value: str) -> str:
        tail = value[-_HINT_CHARS:] if len(value) > _HINT_CHARS else ""
        return "•" * 4 + tail

    @staticmethod
    def _key_of(tenant: str, name: str) -> str:
        return f"{tenant or '*'}|{name}"

    # ── write ─────────────────────────────────────────────────────────────────
    async def set(self, *, tenant: str, name: str, value: str, actor_ref: str = "") -> SecretInfo:
        """Store a credential, keeping the previous one for the rotation window."""
        if not name:
            raise SecretError("a credential needs a name")
        if not value or not value.strip():
            raise SecretError("refusing to store an empty credential — use clear() to remove one, "
                              "so that 'unset' and 'set to nothing' cannot be confused")
        key = self._key_of(tenant, name)
        existing = await self._s.get(self._coll, key)
        now = self._now()
        row = {
            "id": key, "tenant": tenant, "name": name,
            "sealed": self._seal(value),
            "hint": self._hint(value),
            "set_at": str(now), "set_by": actor_ref,
            # The previous value survives briefly so in-flight requests do not fail mid-rotation,
            # and so a mistaken paste is recoverable rather than final.
            "previous": (existing or {}).get("sealed", ""),
            "previous_until": str(now + self._window) if existing else "",
        }
        await self._s.put(self._coll, key, row)
        return SecretInfo(name=name, hint=row["hint"], set_at=row["set_at"],
                          set_by=actor_ref, rotating=bool(row["previous"]))

    async def clear(self, *, tenant: str, name: str) -> bool:
        return await self._s.delete(self._coll, self._key_of(tenant, name))

    # ── read ──────────────────────────────────────────────────────────────────
    async def get(self, *, tenant: str, name: str, fallback: str = "") -> str:
        """The plaintext credential, for IN-PROCESS use by an adapter.

        NEVER return this from an HTTP route. An adapter needs the real value to authenticate, so
        the store cannot be write-only in the literal sense; the write-only guarantee lives at the
        API boundary and is enforced by a gate.

        `fallback` is the configured environment value. Config remains the default and runtime
        overrides it, so a deployment with no stored credential behaves exactly as it does today
        and nothing has to be migrated.
        """
        row = await self._s.get(self._coll, self._key_of(tenant, name))
        if not row:
            # A tenant with no override inherits the platform-wide one before falling back to env.
            if tenant:
                row = await self._s.get(self._coll, self._key_of("", name))
            if not row:
                return fallback
        return self._open(row["sealed"])

    async def accepted(self, *, tenant: str, name: str) -> tuple[str, ...]:
        """Every credential a provider should currently be willing to accept from us.

        The current one, plus the previous while its window is open. Used where WE verify an
        inbound signature — during a rotation both are valid, and rejecting the old one is an
        outage for every caller that has not yet been updated.
        """
        row = await self._s.get(self._coll, self._key_of(tenant, name))
        if not row:
            return ()
        out = [self._open(row["sealed"])]
        prev, until = row.get("previous"), row.get("previous_until")
        if prev and until and self._now() < float(until):
            out.append(self._open(prev))
        return tuple(out)

    async def info(self, *, tenant: str, name: str) -> SecretInfo | None:
        """What the panel may see. No path here reaches the plaintext."""
        row = await self._s.get(self._coll, self._key_of(tenant, name))
        if not row:
            return None
        prev, until = row.get("previous"), row.get("previous_until")
        return SecretInfo(name=row["name"], hint=row.get("hint", ""),
                          set_at=row.get("set_at", ""), set_by=row.get("set_by", ""),
                          rotating=bool(prev and until and self._now() < float(until)))

    async def list_info(self, *, tenant: str) -> list[SecretInfo]:
        rows, _ = await self._s.find(self._coll, {"tenant": tenant}, limit=200, sort=("name", "asc"))
        return [SecretInfo(name=r["name"], hint=r.get("hint", ""), set_at=r.get("set_at", ""),
                           set_by=r.get("set_by", ""),
                           rotating=bool(r.get("previous"))) for r in rows]
