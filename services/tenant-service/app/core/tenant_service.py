"""Core tenant logic — provisioning, resolution, lifecycle (with events), export.

Framework-free; depends only on ports (repository over DataStore, IsolationStrategy,
DomainResolver, EventBus). Lifecycle changes emit domain events so peers can react
(e.g. purge a deleted tenant's data) without any code coupling.
"""
from __future__ import annotations

import re
from typing import Any

from app.core.models import Tenant, new_id
from app.core.tenant_repository import TenantRepository
from app.ports.event_bus import Event

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class InvalidSlug(Exception):
    pass


class TenantService:
    def __init__(self, *, repo: TenantRepository, isolation, resolver, event_bus,
                 default_isolation: str, default_locale: str, default_timezone: str,
                 default_calendar: str, slug_min: int, slug_max: int) -> None:
        self._repo = repo
        self._isolation = isolation
        self._resolver = resolver
        self._bus = event_bus
        self._default_isolation = default_isolation
        self._default_locale = default_locale
        self._default_timezone = default_timezone
        self._default_calendar = default_calendar
        self._slug_min = slug_min
        self._slug_max = slug_max

    def _validate_slug(self, slug: str) -> str:
        if not (self._slug_min <= len(slug) <= self._slug_max) or not _SLUG_RE.match(slug):
            raise InvalidSlug(f"invalid slug: {slug!r}")
        return slug

    async def _emit(self, event_type: str, tenant: Tenant) -> None:
        try:
            await self._bus.publish(event_type, Event(
                type=event_type, payload={"tenant_id": tenant.id, "slug": tenant.slug}))
        except Exception:
            pass  # events are best-effort; never block a lifecycle op

    # ── provisioning ──────────────────────────────────────────────────────────
    async def provision(self, slug: str, name: str, *, domains: list[str] | None = None,
                        isolation_mode: str | None = None,
                        config_overrides: dict[str, Any] | None = None) -> Tenant:
        self._validate_slug(slug)
        config = {
            "locale": self._default_locale, "timezone": self._default_timezone,
            "calendar": self._default_calendar, "feature_flags": {}, "limits": {},
        }
        config.update(config_overrides or {})
        tenant = Tenant(
            id=new_id(), slug=slug, name=name, status="active",
            isolation_mode=isolation_mode or self._default_isolation,
            domains=list(domains or []), config=config,
        )
        await self._repo.create(tenant)
        await self._emit("tenant.created", tenant)
        return tenant

    async def get(self, tenant_id: str) -> Tenant | None:
        return await self._repo.get(tenant_id)

    async def resolve_by_host(self, host: str) -> Tenant | None:
        slug = await self._resolver.tenant_slug_for(host)
        return await self._repo.get_by_slug(slug) if slug else None

    async def list(self, limit: int = 100) -> list[dict]:
        return await self._repo.list(limit)

    # ── config / branding ─────────────────────────────────────────────────────
    async def update_config(self, tenant: Tenant, config: dict[str, Any]) -> Tenant:
        tenant.config.update(config)
        await self._repo.update(tenant)
        await self._emit("tenant.updated", tenant)
        return tenant

    async def set_branding(self, tenant: Tenant, branding: dict[str, Any]) -> Tenant:
        tenant.branding.update(branding)
        await self._repo.update(tenant)
        await self._emit("tenant.updated", tenant)
        return tenant

    # ── lifecycle ──────────────────────────────────────────────────────────────
    async def suspend(self, tenant: Tenant) -> Tenant:
        tenant.status = "suspended"
        await self._repo.update(tenant)
        await self._emit("tenant.suspended", tenant)
        return tenant

    async def reactivate(self, tenant: Tenant) -> Tenant:
        tenant.status = "active"
        await self._repo.update(tenant)
        await self._emit("tenant.reactivated", tenant)
        return tenant

    async def delete(self, tenant: Tenant) -> dict:
        await self._repo.delete(tenant)   # purge this service's tenant records
        await self._emit("tenant.deleted", tenant)  # peers purge their own data
        return {"status": "deleted", "tenant_id": tenant.id, "purged": True}

    # ── offboarding / scope ────────────────────────────────────────────────────
    async def export(self, tenant: Tenant) -> dict:
        return {"tenant": tenant.to_dict(), "scope": self.scope(tenant)}

    def scope(self, tenant: Tenant) -> dict[str, str]:
        return self._isolation.scope(tenant.id, tenant.slug)
