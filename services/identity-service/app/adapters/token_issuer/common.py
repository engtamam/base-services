"""Shared JWT signing via PyJWT (vetted crypto — never hand-rolled)."""
from __future__ import annotations

import datetime as _dt
from typing import Any

# Claims the SIGNER owns. `extra` is a caller-supplied dict, and it used to be merged with a plain
# `payload.update(extra)` — so an `extra` carrying "sub" or "type" would silently rewrite who the
# token is for and what it is allowed to do. Nothing does that today; the mint path is the last
# place in this codebase to leave a loaded gun lying around on the argument that nobody has picked
# it up yet. A caller that tries now gets a crash, not a forged token.
_RESERVED = frozenset({"sub", "iat", "exp", "type", "iss", "aud"})


def encode(
    *, key: str, algorithm: str, subject: str, issuer: str, audience: str,
    ttl_seconds: int, token_type: str, extra: dict[str, Any] | None = None,
    kid: str | None = None,
) -> str:
    import jwt

    now = _dt.datetime.now(_dt.timezone.utc)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": now,
        "exp": now + _dt.timedelta(seconds=ttl_seconds),
        "type": token_type,
    }
    if issuer:
        payload["iss"] = issuer
    if audience:
        payload["aud"] = audience
    if extra:
        collision = _RESERVED.intersection(extra)
        if collision:
            raise ValueError(
                f"refusing to sign: claims {sorted(collision)} are owned by the signer and may "
                f"not be overridden by a caller"
            )
        payload.update(extra)
    # A `kid` names WHICH key signed this, so a verifier can pick the matching public key from a
    # JWKS during a rotation overlap. Omitted (None) on the static single-key path, which keeps the
    # token header byte-for-byte what it was before rotation existed.
    headers = {"kid": kid} if kid else None
    return jwt.encode(payload, key, algorithm=algorithm, headers=headers)
