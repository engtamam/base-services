"""Core authorization logic — PAP (manage roles/policies/assignments) + PDP (decide).

The PDP `check()` is the hottest path in the platform — an authorization decision runs on
effectively every request. Naively it scans the whole policy table and fans out a role
lookup per decision. So we CACHE, keyed by a per-tenant generation counter that any PAP
write bumps (one bump invalidates that tenant's policy set AND every subject's cap set at
once). A cache outage never changes the answer — it only removes the speedup.
"""
from __future__ import annotations

import json
from typing import Any

from app.core.models import Assignment, Policy, Role, new_id
from app.core.stores import AssignmentStore, PolicyStore, RoleStore
from app.ports.policy_engine import Decision


class AuthzService:
    def __init__(self, *, roles: RoleStore, policies: PolicyStore,
                 assignments: AssignmentStore, engine, list_limit: int = 100,
                 cache=None, cache_ttl: int = 30) -> None:
        self._roles = roles
        self._policies = policies
        self._assign = assignments
        self._engine = engine
        self._limit = list_limit
        self._cache = cache
        self._ttl = cache_ttl

    # ── cache (generation-keyed; a PAP write bumps the tenant generation) ───────
    async def _gen(self, tenant: str) -> str:
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"authzgen:{tenant}")) or "0"
        except Exception:
            return "0"

    async def _bump(self, tenant: str) -> None:
        if self._cache is None:
            return
        try:
            gen = await self._cache.get(f"authzgen:{tenant}")
            await self._cache.set(f"authzgen:{tenant}", str(int(gen or 0) + 1), None)
        except Exception:
            pass

    async def _cache_get(self, key: str):
        if self._cache is None:
            return None
        try:
            raw = await self._cache.get(key)
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value) -> None:
        if self._cache is None:
            return
        try:
            await self._cache.set(key, json.dumps(value), self._ttl)
        except Exception:
            pass

    # ── roles (PAP) ────────────────────────────────────────────────────────────
    async def create_role(self, tenant: str, name: str, capabilities: list[str],
                          description: str = "") -> Role:
        role = await self._roles.create(Role(id=new_id(), tenant=tenant, name=name,
                                             capabilities=list(capabilities or []), description=description))
        await self._bump(tenant)
        return role

    async def get_role(self, role_id: str) -> Role | None:
        return await self._roles.get(role_id)

    async def list_roles(self, tenant: str) -> list[dict]:
        return await self._roles.list(tenant, self._limit)

    async def update_role(self, role: Role, capabilities: list[str] | None = None,
                          description: str | None = None) -> Role:
        if capabilities is not None:
            role.capabilities = list(capabilities)
        if description is not None:
            role.description = description
        saved = await self._roles.create(role)
        await self._bump(role.tenant)
        return saved

    async def delete_role(self, role_id: str) -> None:
        role = await self._roles.get(role_id)
        await self._roles.delete(role_id)
        if role:
            await self._bump(role.tenant)

    # ── policies (PAP) ─────────────────────────────────────────────────────────
    async def create_policy(self, tenant: str, effect: str, action: str, resource: str,
                            condition: dict[str, Any] | None = None, priority: int = 0,
                            description: str = "") -> Policy:
        policy = await self._policies.create(Policy(
            id=new_id(), tenant=tenant, effect=effect, action=action, resource=resource,
            condition=condition or {}, priority=priority, description=description))
        await self._bump(tenant)
        return policy

    async def get_policy(self, policy_id: str) -> Policy | None:
        return await self._policies.get(policy_id)

    async def list_policies(self, tenant: str) -> list[dict]:
        return await self._policies.list(tenant, self._limit)

    async def delete_policy(self, policy_id: str) -> None:
        policy = await self._policies.get(policy_id)
        await self._policies.delete(policy_id)
        if policy:
            await self._bump(policy.tenant)

    # ── assignments (PAP) ──────────────────────────────────────────────────────
    async def set_subject_roles(self, subject: str, tenant: str, roles: list[str],
                                capabilities: list[str] | None = None) -> Assignment:
        a = await self._assign.set(Assignment(subject=subject, tenant=tenant,
                                              roles=list(roles or []),
                                              capabilities=list(capabilities or [])))
        await self._bump(tenant)
        return a

    async def get_assignment(self, subject: str, tenant: str) -> Assignment:
        return await self._assign.get(subject, tenant)

    # ── effective capabilities ──────────────────────────────────────────────────
    async def _subject_caps(self, subject: str, tenant: str) -> set[str]:
        """The stored (assignment + role) caps — cacheable, independent of per-request caps."""
        gen = await self._gen(tenant)
        key = f"authz:sc:{gen}:{tenant}:{subject}"
        cached = await self._cache_get(key)
        if cached is not None:
            return set(cached)
        a = await self._assign.get(subject, tenant)
        caps = set(a.capabilities)
        for role_id in a.roles:
            role = await self._roles.get(role_id)
            if role and role.tenant == tenant:
                caps |= set(role.capabilities)
        await self._cache_set(key, sorted(caps))
        return caps

    async def effective(self, subject: str, tenant: str,
                        extra_caps: list[str] | None = None) -> set[str]:
        # extra_caps come from the request token, so they are unioned AFTER the cached set.
        return await self._subject_caps(subject, tenant) | set(extra_caps or [])

    async def _tenant_policies(self, tenant: str) -> list[dict]:
        gen = await self._gen(tenant)
        key = f"authz:pol:{gen}:{tenant}"
        cached = await self._cache_get(key)
        if cached is not None:
            return cached
        policies = await self._policies.list(tenant, 1_000_000)
        await self._cache_set(key, policies)
        return policies

    # ── decision (PDP) — the platform hot path ──────────────────────────────────
    async def check(self, *, subject: str, action: str, resource: str, tenant: str,
                    extra_caps: list[str] | None = None,
                    attributes: dict[str, Any] | None = None) -> Decision:
        caps = await self.effective(subject, tenant, extra_caps)
        policies = await self._tenant_policies(tenant)
        return self._engine.decide({
            "subject": subject, "action": action, "resource": resource,
            "effective_caps": list(caps), "attributes": attributes or {}, "policies": policies,
        })
