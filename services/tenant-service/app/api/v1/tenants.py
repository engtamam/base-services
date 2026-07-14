# =============================================================================
# tenant-service — platform-admin + tenant-self routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.tenant_repository import DomainTaken, SlugTaken, TenantRepository
from app.core.tenant_service import InvalidSlug, TenantService

admin_router = APIRouter(prefix="/api/v1/tenants", tags=["tenants"])
self_router = APIRouter(prefix="/api/v1/tenant", tags=["tenant-self"])

_PLATFORM = require_capability("platform.tenants")
_TENANT_ADMIN = require_capability("tenant.manage")


def build_service(request: Request) -> TenantService:
    c = request.app.state.container
    s = get_settings()
    return TenantService(
        repo=TenantRepository(c.datastore), isolation=c.isolation, resolver=c.domain_resolver,
        event_bus=c.event_bus, default_isolation=s.ISOLATION_DRIVER,
        default_locale=s.DEFAULT_LOCALE, default_timezone=s.DEFAULT_TIMEZONE,
        default_calendar=s.DEFAULT_CALENDAR, slug_min=s.SLUG_MIN_LEN, slug_max=s.SLUG_MAX_LEN,
        delete_mode=s.TENANT_DELETE_MODE, tombstone_fields=s.tombstone_fields,
    )


class ProvisionIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    domains: list[str] = Field(default_factory=list)
    isolation_mode: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)


class ConfigIn(BaseModel):
    config: dict[str, Any]


class BrandingIn(BaseModel):
    branding: dict[str, Any]


async def _load(request: Request, tenant_id: str):
    svc = build_service(request)
    tenant = await svc.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return svc, tenant


# ── Platform admin (cross-tenant) ────────────────────────────────────────────
@admin_router.post("", status_code=201, dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def provision(request: Request, body: ProvisionIn) -> dict:
    svc = build_service(request)
    try:
        t = await svc.provision(body.slug, body.name, domains=body.domains,
                                isolation_mode=body.isolation_mode, config_overrides=body.config)
    except InvalidSlug:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_SLUG")
    except SlugTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "SLUG_TAKEN")
    except DomainTaken:
        raise HTTPException(status.HTTP_409_CONFLICT, "DOMAIN_TAKEN")
    return t.public()


@admin_router.get("", dependencies=[Depends(_PLATFORM)])
async def list_tenants(request: Request) -> dict:
    limit = get_settings().TENANT_LIST_LIMIT
    return {"tenants": await build_service(request).list(limit)}


@admin_router.get("/{tenant_id}", dependencies=[Depends(_PLATFORM)])
async def get_tenant(request: Request, tenant_id: str) -> dict:
    _, tenant = await _load(request, tenant_id)
    return tenant.public()


@admin_router.put("/{tenant_id}/config", dependencies=[Depends(_PLATFORM)])
async def update_config(request: Request, tenant_id: str, body: ConfigIn) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return (await svc.update_config(tenant, body.config)).public()


@admin_router.put("/{tenant_id}/branding", dependencies=[Depends(_PLATFORM)])
async def set_branding(request: Request, tenant_id: str, body: BrandingIn) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return (await svc.set_branding(tenant, body.branding)).public()


@admin_router.post("/{tenant_id}/suspend", dependencies=[Depends(_PLATFORM)])
async def suspend(request: Request, tenant_id: str) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return (await svc.suspend(tenant)).public()


@admin_router.post("/{tenant_id}/reactivate", dependencies=[Depends(_PLATFORM)])
async def reactivate(request: Request, tenant_id: str) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return (await svc.reactivate(tenant)).public()


@admin_router.delete("/{tenant_id}", dependencies=[Depends(_PLATFORM)])
async def delete(request: Request, tenant_id: str) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return await svc.delete(tenant)


@admin_router.get("/{tenant_id}/export", dependencies=[Depends(_PLATFORM)])
async def export(request: Request, tenant_id: str) -> dict:
    svc, tenant = await _load(request, tenant_id)
    return await svc.export(tenant)


# ── Tenant self (scoped to the caller's own tenant) ──────────────────────────
async def _own(request: Request, principal: Principal):
    if not principal.tenant:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")
    return await _load(request, principal.tenant)


@self_router.get("", dependencies=[Depends(_TENANT_ADMIN)])
async def my_tenant(request: Request, principal: Principal = Depends(require_user),
                    guard=Depends(get_guard)) -> dict:
    _, tenant = await _own(request, principal)
    async with guard.step("respond") as step:
        body = tenant.public()
        step.result = body
    return body


@self_router.put("/config", dependencies=[Depends(_TENANT_ADMIN)])
async def my_config(request: Request, body: ConfigIn,
                    principal: Principal = Depends(require_user)) -> dict:
    svc, tenant = await _own(request, principal)
    return (await svc.update_config(tenant, body.config)).public()


@self_router.put("/branding", dependencies=[Depends(_TENANT_ADMIN)])
async def my_branding(request: Request, body: BrandingIn,
                      principal: Principal = Depends(require_user)) -> dict:
    svc, tenant = await _own(request, principal)
    return (await svc.set_branding(tenant, body.branding)).public()
