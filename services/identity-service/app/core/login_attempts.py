"""Per-account brute-force lockout.

The route's generic rate-limit throttles by IP/tenant — it does NOT stop an attacker
credential-stuffing a *single* account from many IPs. This tracks consecutive failures per
(tenant, username) and locks the account after a threshold, for a cooldown. The lock is
keyed on the submitted username, and the caller returns the same generic failure either
way, so it never reveals whether an account exists.
"""
from __future__ import annotations

import datetime as _dt

from app.ports.datastore import DataStore

_COLL = "login_attempts"


def _now() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


class LoginAttemptStore:
    def __init__(self, store: DataStore, *, max_attempts: int = 5,
                 window_seconds: int = 900, lockout_seconds: int = 900) -> None:
        self._store = store
        self._max = max_attempts
        self._window = window_seconds
        self._lockout = lockout_seconds

    @staticmethod
    def _key(tenant: str, username: str) -> str:
        return f"{tenant}::{username.lower()}"

    async def is_locked(self, tenant: str, username: str, *, now: float | None = None) -> bool:
        row = await self._store.get(_COLL, self._key(tenant, username))
        if not row:
            return False
        return float(row.get("locked_until", 0) or 0) > (now if now is not None else _now())

    async def record_failure(self, tenant: str, username: str, *, now: float | None = None) -> bool:
        """Count a failed attempt; lock the account if the threshold is reached. Returns locked?"""
        t = now if now is not None else _now()
        key = self._key(tenant, username)
        row = await self._store.get(_COLL, key) or {"failures": 0, "first_ts": t, "locked_until": 0}
        # a fresh window resets the counter
        if t - float(row.get("first_ts", t) or t) > self._window:
            row = {"failures": 0, "first_ts": t, "locked_until": 0}
        row["failures"] = int(row.get("failures", 0)) + 1
        locked = row["failures"] >= self._max
        if locked:
            row["locked_until"] = t + self._lockout
        await self._store.put(_COLL, key, row)
        return locked

    async def reset(self, tenant: str, username: str) -> None:
        await self._store.delete(_COLL, self._key(tenant, username))
