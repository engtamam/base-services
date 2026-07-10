"""Repositories over the generic DataStore port (tenant-scoped)."""
from __future__ import annotations

from app.core.models import Assignment, Policy, Role
from app.ports.datastore import DataStore

_ROLES = "roles"
_POLICIES = "policies"
_ASSIGN = "assignments"
_SCAN = 1_000_000


class RoleStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, r: Role) -> Role:
        await self._store.put(_ROLES, r.id, r.to_dict())
        return r

    async def get(self, role_id: str) -> Role | None:
        row = await self._store.get(_ROLES, role_id)
        return Role.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_ROLES, _SCAN) if r.get("tenant") == tenant]
        return rows[:limit]

    async def delete(self, role_id: str) -> None:
        await self._store.delete(_ROLES, role_id)


class PolicyStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, p: Policy) -> Policy:
        await self._store.put(_POLICIES, p.id, p.to_dict())
        return p

    async def get(self, policy_id: str) -> Policy | None:
        row = await self._store.get(_POLICIES, policy_id)
        return Policy.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100) -> list[dict]:
        rows = [r for r in await self._store.list(_POLICIES, _SCAN) if r.get("tenant") == tenant]
        rows.sort(key=lambda r: r.get("priority", 0), reverse=True)
        return rows[:limit]

    async def delete(self, policy_id: str) -> None:
        await self._store.delete(_POLICIES, policy_id)


class AssignmentStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(tenant: str, subject: str) -> str:
        return f"{tenant}::{subject}"

    async def get(self, subject: str, tenant: str) -> Assignment:
        row = await self._store.get(_ASSIGN, self._key(tenant, subject))
        return Assignment.from_dict(row) if row else Assignment(subject=subject, tenant=tenant)

    async def set(self, a: Assignment) -> Assignment:
        await self._store.put(_ASSIGN, self._key(a.tenant, a.subject), a.to_dict())
        return a
