# =============================================================================
# authz-service — PAP (roles / policies / assignments) admin routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.authz_service import AuthzService
from app.core.cap_policy import forbidden_caps
from app.core.stores import AssignmentStore, PolicyStore, RoleStore

router = APIRouter(prefix="/api/v1", tags=["authz"])

_ADMIN = require_any_capability("platform.authz", "authz.manage")


def build_authz(request: Request) -> AuthzService:
    c = request.app.state.container
    s = get_settings()
    return AuthzService(roles=RoleStore(c.datastore), policies=PolicyStore(c.datastore),
                        assignments=AssignmentStore(c.datastore), engine=c.policy_engine,
                        list_limit=s.AUTHZ_LIST_LIMIT, cache=c.cache,
                        cache_ttl=s.AUTHZ_CACHE_TTL_SECONDS,
                        role_lookup=s.AUTHZ_ROLE_LOOKUP, unknown_role=s.AUTHZ_UNKNOWN_ROLE)


def _op_tenant(principal: Principal, body_tenant: str | None = None) -> str:
    if "platform.authz" in principal.caps:
        return body_tenant or principal.tenant or "global"
    return principal.tenant or "__none__"   # authz.manage: forced to own tenant


def _guard_tenant(principal: Principal, obj_tenant: str) -> None:
    if "platform.authz" in principal.caps:
        return
    if obj_tenant != (principal.tenant or "__none__"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")


def _guard_caps(principal: Principal, caps: list[str] | None) -> None:
    """Refuse a tenant-scoped caller assigning capabilities that escalate beyond their tenant.

    Isolating the ROW to the tenant (`_op_tenant`) is not isolating the POWER the row grants: without
    this, an `authz.manage` holder could put `platform.*` / `authz.manage` in a role and assign it to
    themselves. A platform operator (`platform.authz`) is unrestricted — delegated administration is
    their job. Enforced server-side, at every route that accepts `capabilities`.
    """
    if caps is None:
        return
    s = get_settings()
    bad = forbidden_caps(
        caps, is_operator="platform.authz" in principal.caps,
        denied_prefixes=s.authz_tenant_denied_cap_prefixes,
        denied_caps=s.authz_tenant_denied_caps,
        allowlist=s.authz_tenant_assignable_caps)
    if bad:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            f"capabilities not assignable by a tenant admin: {', '.join(bad)}")


class RoleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    capabilities: list[str] = Field(default_factory=list)
    description: str = ""
    tenant: str | None = None


class RoleUpdate(BaseModel):
    capabilities: list[str] | None = None
    description: str | None = None


class PolicyIn(BaseModel):
    effect: str = Field(pattern="^(allow|deny)$")
    action: str = "*"
    resource: str = "*"
    condition: dict[str, Any] = Field(default_factory=dict)
    priority: int = 0
    description: str = ""
    tenant: str | None = None


class AssignIn(BaseModel):
    roles: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)


# ── roles ────────────────────────────────────────────────────────────────────
@router.post("/roles", status_code=201)
async def create_role(request: Request, body: RoleIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Reject an escalating capability set BEFORE anything else — it is neither a mutation to record
    # nor an audited attempt, it is a request that must not be honoured at all.
    _guard_caps(principal, body.capabilities)
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "role.created", actor=principal.subject, tenant=body.tenant or principal.tenant,
        resource="role",
        ip=request.client.host if request.client else "")
    tenant = _op_tenant(principal, body.tenant)
    r = await build_authz(request).create_role(tenant, body.name, body.capabilities, body.description)
    return r.to_dict()


@router.get("/roles")
async def list_roles(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return {"roles": await build_authz(request).list_roles(_op_tenant(principal))}


@router.get("/roles/{role_id}")
async def get_role(request: Request, role_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    role = await build_authz(request).get_role(role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, role.tenant)
    return role.to_dict()


@router.put("/roles/{role_id}")
async def update_role(request: Request, role_id: str, body: RoleUpdate,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    _guard_caps(principal, body.capabilities)
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "role.updated", actor=principal.subject, tenant=principal.tenant, resource=f"role:{role_id}",
        ip=request.client.host if request.client else "")
    svc = build_authz(request)
    role = await svc.get_role(role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, role.tenant)
    return (await svc.update_role(role, body.capabilities, body.description)).to_dict()


@router.delete("/roles/{role_id}")
async def delete_role(request: Request, role_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "role.deleted", actor=principal.subject, tenant=principal.tenant, resource=f"role:{role_id}",
        ip=request.client.host if request.client else "")
    svc = build_authz(request)
    role = await svc.get_role(role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, role.tenant)
    await svc.delete_role(role_id)
    return {"deleted": True}


# ── policies ──────────────────────────────────────────────────────────────────
@router.post("/policies", status_code=201)
async def create_policy(request: Request, body: PolicyIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "policy.created", actor=principal.subject, tenant=principal.tenant, resource="policy",
        ip=request.client.host if request.client else "")
    tenant = _op_tenant(principal, body.tenant)
    p = await build_authz(request).create_policy(tenant, body.effect, body.action, body.resource,
                                                 body.condition, body.priority, body.description)
    return p.to_dict()


@router.get("/policies")
async def list_policies(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return {"policies": await build_authz(request).list_policies(_op_tenant(principal))}


@router.get("/policies/{policy_id}")
async def view_policy(request: Request, policy_id: str,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    """One policy's full detail — effect, action, resource, condition, priority. The see-before-you-
    delete read: policies could be listed and deleted but never inspected on their own."""
    p = await build_authz(request).get_policy(policy_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, p.tenant)
    return p.to_dict()


@router.delete("/policies/{policy_id}")
async def delete_policy(request: Request, policy_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "policy.deleted", actor=principal.subject, tenant=principal.tenant, resource=f"policy:{policy_id}",
        ip=request.client.host if request.client else "")
    svc = build_authz(request)
    p = await svc.get_policy(policy_id)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, p.tenant)
    await svc.delete_policy(policy_id)
    return {"deleted": True}


# ── assignments ───────────────────────────────────────────────────────────────
@router.put("/subjects/{subject}/roles")
async def set_roles(request: Request, subject: str, body: AssignIn,
                    principal: Principal = Depends(_ADMIN)) -> dict:
    # The most direct escalation vector: a DIRECT capability grant to a subject, no role in between.
    _guard_caps(principal, body.capabilities)
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.roles_assigned", actor=principal.subject, tenant=principal.tenant, resource=f"subject:{subject}",
        ip=request.client.host if request.client else "",
        # WHAT was granted — a dispute over "who could do X, and since when" needs the grant itself,
        # not just that some assignment happened. This is the set_roles REPLACE, so it is the new state.
        metadata={"roles": body.roles, "capabilities": body.capabilities})
    tenant = _op_tenant(principal)
    a = await build_authz(request).set_subject_roles(subject, tenant, body.roles, body.capabilities)
    return a.to_dict()


@router.get("/subjects/{subject}/roles")
async def get_roles(request: Request, subject: str, principal: Principal = Depends(_ADMIN)) -> dict:
    return (await build_authz(request).get_assignment(subject, _op_tenant(principal))).to_dict()


@router.get("/subjects/{subject}/effective")
async def subject_effective(request: Request, subject: str,
                            principal: Principal = Depends(_ADMIN)) -> dict:
    """What a subject can ACTUALLY do — the resolved capability set from its roles and direct grants.
    'View a subject's roles' shows the inputs; this shows the derived answer, the read an operator
    needs to debug 'why can/can't this person do X'. The same resolution the internal PDP uses, on
    the panel. Roles are the input, capabilities are derived."""
    tenant = _op_tenant(principal)
    caps = await build_authz(request).effective(subject, tenant)
    return {"subject": subject, "tenant": tenant, "capabilities": sorted(caps)}
