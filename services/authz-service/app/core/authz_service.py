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
from app.ports.errors import ConfigError
from app.ports.policy_engine import Decision

# How a role reference is resolved. Callers naturally hold the NAME (`compliance-manager` is what
# a human assigned); only authz knows the generated id. Accepting both is what makes the common
# mistake simply work.
BY_ID, BY_NAME, BY_BOTH = "id", "name", "both"
ROLE_LOOKUPS = (BY_ID, BY_NAME, BY_BOTH)

# What to do with a role we cannot resolve. REJECT is the default: empty capabilities must mean
# "he holds none", never "I did not understand you".
REJECT, IGNORE = "reject", "ignore"
UNKNOWN_ROLE_MODES = (REJECT, IGNORE)


class UnknownRole(Exception):
    """A role reference resolved to nothing. Answered 4xx — never 200 with an empty set."""


class AuthzService:
    def __init__(self, *, roles: RoleStore, policies: PolicyStore,
                 assignments: AssignmentStore, engine, list_limit: int = 100,
                 cache=None, cache_ttl: int = 30,
                 role_lookup: str = BY_BOTH, unknown_role: str = REJECT) -> None:
        self._roles = roles
        self._policies = policies
        self._assign = assignments
        self._engine = engine
        self._limit = list_limit
        self._cache = cache
        self._ttl = cache_ttl
        if role_lookup not in ROLE_LOOKUPS:
            raise ConfigError(f"unknown AUTHZ_ROLE_LOOKUP {role_lookup!r} — "
                              f"allowed: {', '.join(ROLE_LOOKUPS)}")
        if unknown_role not in UNKNOWN_ROLE_MODES:
            raise ConfigError(f"unknown AUTHZ_UNKNOWN_ROLE {unknown_role!r} — "
                              f"allowed: {', '.join(UNKNOWN_ROLE_MODES)}")
        self._role_lookup = role_lookup
        self._unknown_role = unknown_role

    # ── cache (generation-keyed; a PAP write bumps the tenant generation) ───────
    async def _gen(self, tenant: str) -> str | None:
        """The tenant's cache generation. None means the cache is UNUSABLE — callers must then BYPASS
        the cache and recompute from the store (FIN authz-003). Returning "0" on a cache error was
        fail-OPEN: a transient blip after a revoke bumped the gen to e.g. "5" would key the lookup back
        at gen "0" and resurrect a stale pre-revoke entry — serving revoked capabilities. No cache
        configured is a stable "0" (nothing is ever cached, so nothing can be stale)."""
        if self._cache is None:
            return "0"
        try:
            return (await self._cache.get(f"authzgen:{tenant}")) or "0"
        except Exception:
            return None      # cache down → do NOT trust any cached decision; recompute from source

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

    async def list_assignments(self, tenant: str) -> list[dict]:
        """Every subject's roles + direct capabilities for one tenant — the staff/access report view.
        Current state only (the rows carry no history); tenant-scoped via the store's indexed find."""
        return await self._assign.all_for(tenant)

    # ── effective capabilities ──────────────────────────────────────────────────
    async def _subject_caps(self, subject: str, tenant: str) -> set[str]:
        """The stored (assignment + role) caps — cacheable, independent of per-request caps."""
        gen = await self._gen(tenant)
        key = f"authz:sc:{gen}:{len(tenant)}:{tenant}:{subject}"  # len-prefixed: no (tenant,subject) collision
        if gen is not None:                       # gen None → cache unusable → recompute from store
            cached = await self._cache_get(key)
            if cached is not None:
                return set(cached)
        a = await self._assign.get(subject, tenant)
        caps = set(a.capabilities)
        for role_id in a.roles:
            role = await self._roles.get(role_id)
            if role and role.tenant == tenant:
                caps |= set(role.capabilities)
        if gen is not None:
            await self._cache_set(key, sorted(caps))
        return caps

    async def _role_caps(self, tenant: str, roles: list[str]) -> set[str]:
        """Expand roles the CALLER holds into capabilities. Cached per (tenant, role).

        This is what makes a role a role. Identity owns "Sara is a compliance-manager at acme" —
        it is a fact about her employment, and it lives on her membership row. Authz owns "a
        compliance-manager may do X, Y, Z" — that is policy, and it lives here. Identity passes
        the roles in; we say what they mean.

        Without this, the two stores each hold half of an answer and neither can give it: authz
        can only expand roles that were separately assigned INTO authz, which nothing does, so it
        would return the empty set for every real user on the platform.

        A role that does not exist, or belongs to another tenant, contributes NOTHING. It is never
        an error the caller can use to discover which roles exist in someone else's company, and it
        never silently grants.
        """
        caps: set[str] = set()
        gen = await self._gen(tenant)               # None → cache unusable → recompute from store
        for ref in roles or []:
            key = f"authz:rc:{gen}:{len(tenant)}:{tenant}:{ref}"  # len-prefixed: no (tenant,ref) collision
            if gen is not None:
                cached = await self._cache_get(key)
                if cached is not None:
                    caps |= set(cached)
                    continue
            role = await self._resolve_role(tenant, ref)
            if role is None:
                # EMPTY MUST MEAN "HE HOLDS NONE" — NEVER "I DID NOT UNDERSTAND YOU".
                #
                # This used to skip silently, so an unresolvable role contributed nothing and the
                # caller got 200 with an empty capability set: a correct answer to a question
                # asked in the wrong vocabulary. Identity then minted a token that could do
                # nothing and every screen 403'd, with no error anywhere to explain why.
                #
                # Now it is loud. Identity fails CLOSED on our 4xx, so the operator sees a
                # refusal instead of a user who mysteriously cannot do their job.
                if self._unknown_role == REJECT:
                    raise UnknownRole(ref)
                if gen is not None:
                    await self._cache_set(key, [])
                continue
            granted = set(role.capabilities)
            if gen is not None:
                await self._cache_set(key, sorted(granted))
            caps |= granted
        return caps

    async def _resolve_role(self, tenant: str, ref: str) -> Role | None:
        """A role reference is an id or a NAME. Callers naturally hold the name.

        The tenant check is the wall, and it applies to BOTH lookups: a role belonging to another
        company resolves to nothing here, so a copied id or a duplicated name cannot import that
        company's privileges into this one.
        """
        if self._role_lookup in (BY_ID, BY_BOTH):
            role = await self._roles.get(ref)
            if role is not None and role.tenant == tenant:
                return role
        if self._role_lookup in (BY_NAME, BY_BOTH):
            role = await self._roles.by_name(tenant, ref)
            if role is not None:
                return role
        return None

    async def effective(self, subject: str, tenant: str,
                        extra_caps: list[str] | None = None,
                        roles: list[str] | None = None) -> set[str]:
        # extra_caps come from the request token, so they are unioned AFTER the cached set.
        # `roles` are the roles the CALLER (identity) holds for this subject — the input side of
        # "roles are the input, caps are derived".
        return (await self._subject_caps(subject, tenant)
                | await self._role_caps(tenant, roles or [])
                | set(extra_caps or []))

    async def _tenant_policies(self, tenant: str) -> list[dict]:
        gen = await self._gen(tenant)               # None → cache unusable → read from store
        key = f"authz:pol:{gen}:{tenant}"
        if gen is not None:
            cached = await self._cache_get(key)
            if cached is not None:
                return cached
        policies = await self._policies.all_for(tenant)
        if gen is not None:
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
