"""RefreshStore — tracks refresh-token jti for rotation + revocation (replay defense).

Backed by the generic DataStore port. A refresh token is valid only while its
jti is present, unrevoked, and unexpired. Rotation revokes the old jti.
"""
from __future__ import annotations

import datetime as _dt

from app.ports.datastore import DataStore

_COLL = "refresh_tokens"
_PAGE = 500                # revocation pages to exhaustion; this is the page, not a cap


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
        """Revoke EVERY refresh token for a user - the response to a detected token reuse.

        This is a SECURITY path, and the old implementation was the most dangerous read in the
        repository. It scanned up to a million refresh tokens across every user and filtered in
        Python. A `LIMIT` without an `ORDER BY` does not return the first N rows; it returns an
        arbitrary N. Past that cap, "revoke every token for this user" would revoke SOME of
        them, report success, and leave the attacker's stolen token working.

        Now the user and the not-yet-revoked state are pushed into the datastore, and the loop
        pages to exhaustion. It revokes all of them, or it raises. It never reports a number it
        did not do.
        """
        revoked = 0
        cursor = None
        while True:
            rows, cursor = await self._store.find(_COLL, {"user_id": user_id, "revoked": False},
                                                  limit=_PAGE, after=cursor, sort=("jti", "asc"))
            for row in rows:
                if not row.get("jti"):
                    continue
                row["revoked"] = True
                await self._store.put(_COLL, row["jti"], row)
                revoked += 1
            if not cursor:
                break
            # Revoked rows drop out of the filter as we write them, so the keyset cursor is
            # what keeps this terminating: it advances past the rows already visited rather
            # than re-reading a set that is shrinking underneath it.
        return revoked
