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

    async def issue(self, jti: str, user_id: str, exp_ts: float,
                    tenant: str | None = None) -> None:
        """Record a refresh token, and — once a person can span tenants — WHICH tenant it is for.

        A refresh row used to store `user_id` and no tenant at all. That is fine while a user IS
        a tenant, and it becomes the most dangerous line in the service the moment they are not:
        refresh would mint a fresh access token for whichever tenant it felt like, including one
        the person was removed from last week. The tenant is written down here so that refresh can
        re-verify the membership is still current (see AuthService.refresh, T4).

        `tenant=None` is the single-tenant path, unchanged.
        """
        row = {"jti": jti, "user_id": user_id, "exp": exp_ts, "revoked": False}
        if tenant:
            row["tenant"] = tenant
        await self._store.put(_COLL, jti, row)

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

    async def tenant_of(self, jti: str) -> str | None:
        """The tenant this refresh token was minted for. None on the single-tenant path."""
        row = await self._store.get(_COLL, jti)
        return row.get("tenant") if row else None

    async def revoke(self, jti: str) -> None:
        row = await self._store.get(_COLL, jti)
        if row:
            row["revoked"] = True
            # atomic: ok — a jti row is written once at mint then only ever revoked; revoke is
            # idempotent (sets revoked=True), and rotation mints a NEW jti rather than rewriting this
            # one, so there is no competing writer to lose to.
            await self._store.put(_COLL, jti, row)

    async def consume(self, jti: str) -> bool:
        """Atomically ROTATE one refresh jti from active → revoked, returning True for the SINGLE
        winner and False for everyone else (already revoked, expired, unknown, or a concurrent
        caller who won the CAS).

        This is the fix for the rotation TOCTOU: `revoke()` is a get-then-put with no version
        guard, so two concurrent uses of the SAME refresh token could both read it active, both
        revoke it, and both mint a fresh family — reuse detection never firing. `consume` flips the
        row under a compare-and-set, so exactly one rotation wins; the loser is a concurrent reuse
        of one token and the caller treats it as theft (revoke_family). The CAS retries only when it
        loses to a writer that left the row still active (never seen in practice, since the only
        transition is active→revoked), so a lost race resolves to False, not a spurious win."""
        for _ in range(64):
            row = await self._store.get(_COLL, jti)
            if not row:
                return False
            ver = int(row.get("version", 0))
            if row.get("revoked") or float(row.get("exp", 0)) <= _now_ts():
                return False                       # already rotated/expired → not our win
            row["revoked"] = True
            row.pop("version", None)               # cas stamps the next version
            if await self._store.cas(_COLL, jti, ver, row):
                return True
        return False

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
