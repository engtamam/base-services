"""Invites — how a person who does not exist yet gets a password an admin never knows.

THE ATTACK THIS EXISTS TO STOP.

Let a tenant admin choose the initial password for a brand-new person and they can pre-register
`cfo@bank.com` — an identifier they do not control — with a password they know. Later, when that
human is genuinely hired, the identifier collides and the platform correctly refuses to merge two
humans (T7). But it is too late: the admin already owns that login, and the membership at the bank
is granted to THEIR account. It is T2 walking in through the front door, dressed as onboarding.

So in the default mode an admin can create a person but cannot authenticate as one. The Person is
created with an UNUSABLE password hash and a one-time invite token is returned once. Only the human
holding that token sets the password, and they set it to something the admin never sees.

The token is stored HASHED. A leaked invites table is then a table of useless digests rather than a
set of live keys to accounts that have not been claimed yet — the same reason we never store a
password.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import hmac
import secrets

_COLL = "invites"


def _now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class InviteStore:
    def __init__(self, store) -> None:
        self._store = store

    async def issue(self, person_id: str, tenant: str, ttl_seconds: int) -> str:
        """Mint a single-use invite. The RAW token is returned exactly once and never stored."""
        token = secrets.token_urlsafe(32)
        await self._store.put(_COLL, _digest(token), {
            "id": _digest(token),
            "person_id": person_id,
            "tenant": tenant,
            "exp": _now_ts() + ttl_seconds,
            "used": False,
        })
        return token

    async def redeem(self, token: str) -> dict | None:
        """Consume the invite. Returns the row, or None if unknown, expired or already used.

        Single-use is enforced by WRITING `used` before the caller acts on it. An invite that could
        be redeemed twice is a password reset for anyone who ever saw the link.
        """
        row = await self._store.get(_COLL, _digest(token))
        if not row:
            return None
        # Compare in constant time — the digest lookup already did the work, but a caller that
        # learns "this prefix exists" from timing learns something.
        if not hmac.compare_digest(str(row.get("id", "")), _digest(token)):
            return None
        if row.get("used") or float(row.get("exp", 0)) <= _now_ts():
            return None
        row["used"] = True
        await self._store.put(_COLL, row["id"], row)
        return row
