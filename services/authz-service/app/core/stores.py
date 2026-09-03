"""Repositories over the generic DataStore port (tenant-scoped)."""
from __future__ import annotations

from app.core.models import Assignment, Policy, Role
from app.ports.datastore import DataStore

_ROLES = "roles"
_POLICIES = "policies"
_ASSIGN = "assignments"
_PAGE = 500          # the policy set pages; it is never read with a million-row limit


class RoleStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, r: Role) -> Role:
        await self._store.put(_ROLES, r.id, r.to_dict())
        return r

    async def get(self, role_id: str) -> Role | None:
        row = await self._store.get(_ROLES, role_id)
        return Role.from_dict(row) if row else None

    async def by_name(self, tenant: str, name: str) -> Role | None:
        """Resolve a role by its NAME within one tenant.

        Callers naturally hold the name — `compliance-manager` is what a human assigned and what
        the membership row carries. Only authz knows the generated id. Without this, passing a
        name where an id was expected silently resolved to nothing, and the caller got a correct
        answer to a question asked in the wrong vocabulary.

        Pushed down to the store, scoped to the tenant: a role name from another company can never
        resolve here, so a copied name cannot import that company's privileges.
        """
        rows, _ = await self._store.find(_ROLES, {"tenant": tenant, "name": name}, limit=1,
                                         sort=("name", "asc"))
        return Role.from_dict(rows[0]) if rows else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_ROLES, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("name", "asc"))
        return rows

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

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        """A tenant's policies, highest priority first.

        The decision path does NOT call this - it reads the compiled, cached policy set. This is
        the admin view, and it no longer reads every other tenant's policies to build it.
        """
        rows, _ = await self._store.find(_POLICIES, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        rows.sort(key=lambda r: r.get("priority", 0), reverse=True)
        return rows

    async def all_for(self, tenant: str) -> list[dict]:
        """EVERY policy of one tenant, for the PDP to compile and cache.

        The decision path needs the whole set - a policy it does not see is a DENY it does not
        apply, or an ALLOW it wrongly grants. So this pages to exhaustion rather than passing a
        limit large enough to look like "all": that limit was 1_000_000, and had a tenant ever
        exceeded it the PDP would have made decisions from an arbitrary subset of its policies,
        silently.
        """
        out: list[dict] = []
        cursor = None
        while True:
            rows, cursor = await self._store.find(_POLICIES, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("created", "desc"))
            out.extend(rows)
            if not cursor:
                break
        out.sort(key=lambda r: r.get("priority", 0), reverse=True)
        return out

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

    async def all_for(self, tenant: str) -> list[dict]:
        """EVERY assignment of one tenant — who holds what, for the staff/access dispute report.

        Not a decision path (the PDP resolves one subject at a time via effective()); this is the
        admin/report view. Pages to exhaustion over the indexed tenant find — never scan_all — so it
        stays tenant-scoped and cannot walk another tenant's rows, and never silently truncates.
        """
        out: list[dict] = []
        cursor = None
        while True:
            rows, cursor = await self._store.find(_ASSIGN, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("subject", "asc"))
            out.extend(rows)
            if not cursor:
                break
        return out
