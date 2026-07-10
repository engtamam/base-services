"""RefreshStore — tracks refresh-token jti for rotation + revocation (replay defense).

Backed by the generic DataStore port. A refresh token is valid only while its
jti is present, unrevoked, and unexpired. Rotation revokes the old jti.
"""
from __future__ import annotations

import datetime as _dt

from app.ports.datastore import DataStore

_COLL = "refresh_tokens"


def _now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


class RefreshStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def issue(self, jti: str, user_id: str, exp_ts: float) -> None:
        await self._store.put(_COLL, jti, {"jti": jti, "user_id": user_id,
                                           "exp": exp_ts, "revoked": False})

    async def is_active(self, jti: str) -> bool:
        return await self.status(jti) == "active"

    async def status(self, jti: str) -> str:
        """active | revoked | unknown — 'revoked' distinguishes a REUSED (rotated) token."""
        row = await self._store.get(_COLL, jti)
        if not row:
            return "unknown"
        if row.get("revoked") or float(row.get("exp", 0)) <= _now_ts():
            return "revoked"
        return "active"

    async def user_of(self, jti: str) -> str | None:
        row = await self._store.get(_COLL, jti)
        return row.get("user_id") if row else None

    async def revoke(self, jti: str) -> None:
        row = await self._store.get(_COLL, jti)
        if row:
            row["revoked"] = True
            await self._store.put(_COLL, jti, row)

    async def revoke_family(self, user_id: str) -> int:
        """Revoke EVERY refresh token for a user — the response to a detected token reuse."""
        revoked = 0
        for row in await self._store.list(_COLL, 1_000_000):
            if row.get("user_id") == user_id and not row.get("revoked") and row.get("jti"):
                row["revoked"] = True
                await self._store.put(_COLL, row["jti"], row)
                revoked += 1
        return revoked
