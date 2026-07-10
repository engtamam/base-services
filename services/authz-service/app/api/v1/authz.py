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
from app.core.stores import AssignmentStore, PolicyStore, RoleStore

router = APIRouter(prefix="/api/v1", tags=["authz"])

_ADMIN = require_any_capability("platform.authz", "authz.manage")


def build_authz(request: Request) -> AuthzService:
    c = request.app.state.container
    s = get_settings()
    return AuthzService(roles=RoleStore(c.datastore), policies=PolicyStore(c.datastore),
                        assignments=AssignmentStore(c.datastore), engine=c.policy_engine,
                        list_limit=s.AUTHZ_LIST_LIMIT, cache=c.cache,
                        cache_ttl=s.AUTHZ_CACHE_TTL_SECONDS)


def _op_tenant(principal: Principal, body_tenant: str | None = None) -> str:
    if "platform.authz" in principal.caps:
        return body_tenant or principal.tenant or "global"
    return principal.tenant or "__none__"   # authz.manage: forced to own tenant


def _guard_tenant(principal: Principal, obj_tenant: str) -> None:
    if "platform.authz" in principal.caps:
        return
    if obj_tenant != (principal.tenant or "__none__"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")


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
    svc = build_authz(request)
    role = await svc.get_role(role_id)
    if role is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    _guard_tenant(principal, role.tenant)
    return (await svc.update_role(role, body.capabilities, body.description)).to_dict()


@router.delete("/roles/{role_id}")
async def delete_role(request: Request, role_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
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
    tenant = _op_tenant(principal, body.tenant)
    p = await build_authz(request).create_policy(tenant, body.effect, body.action, body.resource,
                                                 body.condition, body.priority, body.description)
    return p.to_dict()


@router.get("/policies")
async def list_policies(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    return {"policies": await build_authz(request).list_policies(_op_tenant(principal))}


@router.delete("/policies/{policy_id}")
async def delete_policy(request: Request, policy_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
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
    tenant = _op_tenant(principal)
    a = await build_authz(request).set_subject_roles(subject, tenant, body.roles, body.capabilities)
    return a.to_dict()


@router.get("/subjects/{subject}/roles")
async def get_roles(request: Request, subject: str, principal: Principal = Depends(_ADMIN)) -> dict:
    return (await build_authz(request).get_assignment(subject, _op_tenant(principal))).to_dict()
