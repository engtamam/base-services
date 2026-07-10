"""TenantRepository over the generic DataStore port (id + slug + domain indexes)."""
from __future__ import annotations

from app.adapters.resolver.resolvers import normalize_host
from app.core.models import Tenant
from app.ports.datastore import DataStore

_TENANTS = "tenants"
_SLUGS = "tenant_slugs"       # slug -> {id}
_DOMAINS = "tenant_domains"   # host -> {slug}


class SlugTaken(Exception):
    pass


class DomainTaken(Exception):
    pass


class TenantRepository:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def create(self, tenant: Tenant) -> Tenant:
        if await self._store.get(_SLUGS, tenant.slug) is not None:
            raise SlugTaken(f"slug already taken: {tenant.slug}")
        for host in map(normalize_host, tenant.domains):
            if await self._store.get(_DOMAINS, host) is not None:
                raise DomainTaken(f"domain already mapped: {host}")
        await self._store.put(_TENANTS, tenant.id, tenant.to_dict())
        await self._store.put(_SLUGS, tenant.slug, {"id": tenant.id})
        for host in map(normalize_host, tenant.domains):
            await self._store.put(_DOMAINS, host, {"slug": tenant.slug})
        return tenant

    async def get(self, tenant_id: str) -> Tenant | None:
        row = await self._store.get(_TENANTS, tenant_id)
        return Tenant.from_dict(row) if row else None

    async def get_by_slug(self, slug: str) -> Tenant | None:
        idx = await self._store.get(_SLUGS, slug)
        return await self.get(idx["id"]) if idx else None

    async def get_by_domain(self, host: str) -> Tenant | None:
        idx = await self._store.get(_DOMAINS, normalize_host(host))
        return await self.get_by_slug(idx["slug"]) if idx else None

    async def update(self, tenant: Tenant, *, old_domains: list[str] | None = None) -> Tenant:
        tenant.touch()
        # re-index domains (remove old, add current)
        for host in map(normalize_host, old_domains or []):
            if host not in map(normalize_host, tenant.domains):
                await self._store.delete(_DOMAINS, host)
        for host in map(normalize_host, tenant.domains):
            await self._store.put(_DOMAINS, host, {"slug": tenant.slug})
        await self._store.put(_TENANTS, tenant.id, tenant.to_dict())
        return tenant

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: this IS the tenant catalogue. There is no tenant to scope it by, and it is
        # reachable only by a platform operator. Bounded by the caller's limit.
        return await self._store.scan_all(_TENANTS, limit)

    async def delete(self, tenant: Tenant) -> None:
        await self._store.delete(_TENANTS, tenant.id)
        await self._store.delete(_SLUGS, tenant.slug)
        for host in map(normalize_host, tenant.domains):
            await self._store.delete(_DOMAINS, host)
