"""UserRepository — persists Users via the generic DataStore port.

Works on memory today and postgres by config (no storage code changes). Keeps a
tenant-scoped username -> id index in the same store for lookups + uniqueness.
"""
from __future__ import annotations

from app.core.models import User
from app.ports.datastore import DataStore
from app.ports.login_identifier import LoginIdentifier

_USERS = "users"
_INDEX = "users_by_username"


class UserExists(Exception):
    pass


class UserRepository:
    def __init__(self, store: DataStore, identifier: LoginIdentifier) -> None:
        self._store = store
        self._id = identifier          # owns normalization; the index key comes from it

    def _index_key(self, tenant: str, raw: str) -> str:
        # Uniqueness AND lookup both key on the SAME normalized form, so two spellings of one
        # identity cannot become two accounts. This replaced a hardcoded `.lower()`.
        return f"{tenant}::{self._id.normalize(raw)}"

    async def create(self, user: User) -> User:
        idx = self._index_key(user.tenant, user.username)
        if await self._store.get(_INDEX, idx) is not None:
            raise UserExists(f"username already taken in tenant: {user.username}")
        await self._store.put(_USERS, user.id, user.to_dict())
        await self._store.put(_INDEX, idx, {"id": user.id})
        return user

    async def get(self, user_id: str) -> User | None:
        row = await self._store.get(_USERS, user_id)
        return User.from_dict(row) if row else None

    async def get_by_username(self, tenant: str, username: str) -> User | None:
        idx = await self._store.get(_INDEX, self._index_key(tenant, username))
        if not idx:
            return None
        return await self.get(idx["id"])

    async def update(self, user: User) -> User:
        user.touch()
        await self._store.put(_USERS, user.id, user.to_dict())
        return user

    async def delete(self, user: User) -> None:
        """Remove the user AND the lookup index — the index IS the linkage.

        Deleting the row but leaving the index would leave `tenant::alice@x.com -> id` behind:
        the person's email, still stored, still enumerable. The index must go first.
        """
        await self._store.delete(_INDEX, self._index_key(user.tenant, user.username))
        await self._store.delete(_USERS, user.id)
