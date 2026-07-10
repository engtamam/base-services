"""UserRepository — persists Users via the generic DataStore port.

Works on memory today and postgres by config (no storage code changes). Keeps a
tenant-scoped username -> id index in the same store for lookups + uniqueness.
"""
from __future__ import annotations

from app.core.models import User
from app.ports.datastore import DataStore

_USERS = "users"
_INDEX = "users_by_username"


class UserExists(Exception):
    pass


class UserRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _index_key(tenant: str, username: str) -> str:
        return f"{tenant}::{username.lower()}"

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
