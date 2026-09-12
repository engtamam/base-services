"""TenantRepository over the generic DataStore port (id + slug + domain indexes)."""
from __future__ import annotations

from app.adapters.resolver.resolvers import normalize_host
from app.core.models import DELETED, SUSPENDED, Tenant, now as _now
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
        # ATOMICALLY reserve the slug and each domain (cas insert-if-absent). The old check-then-put
        # had an await gap, so two concurrent provisions of the same slug/domain both passed the get
        # and both put — two tenants sharing a slug/domain. On any conflict we roll back what we
        # reserved so a failed provision leaves no orphaned index rows.
        if not await self._store.cas(_SLUGS, tenant.slug, 0, {"id": tenant.id}):
            raise SlugTaken(f"slug already taken: {tenant.slug}")
        reserved: list[str] = []
        for host in map(normalize_host, tenant.domains):
            if not await self._store.cas(_DOMAINS, host, 0, {"slug": tenant.slug}):
                await self._store.delete(_SLUGS, tenant.slug)
                for h in reserved:
                    await self._store.delete(_DOMAINS, h)
                raise DomainTaken(f"domain already mapped: {host}")
            reserved.append(host)
        await self._store.put(_TENANTS, tenant.id, tenant.to_dict())
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

    async def iter_suspended(self, limit: int = 200, after: str | None = None):
        """A page of SUSPENDED tenants (the close-sweep's candidate set) — pushed into the store as a
        filter so the sweep reaches EVERY closing tenant over its pages, not just the first N rows of
        the whole catalogue."""
        return await self._store.find(_TENANTS, {"status": SUSPENDED}, limit=limit, after=after,
                                      sort=("created", "asc"))

    async def delete(self, tenant: Tenant) -> None:
        """PURGE. The row is gone and nothing can ever learn it existed. See `tombstone()`."""
        await self._store.delete(_TENANTS, tenant.id)
        await self._store.delete(_SLUGS, tenant.slug)
        for host in map(normalize_host, tenant.domains):
            await self._store.delete(_DOMAINS, host)

    async def tombstone(self, tenant: Tenant, *, keep: tuple[str, ...]) -> Tenant:
        """Delete the tenant, but leave a MARKER that says so.

        A purge and a fire-and-forget bus are a bad pair. `tenant.deleted` is published at most
        once (see the EventBus port): a peer that is restarting when it fires never hears about it,
        and after the purge there is nothing left to ask — GET /internal/tenants/{id} just 404s,
        for ever. Peers fail OPEN on 404, and reasonably so (an unknown tenant is usually an
        onboarding gap, not a punishment), so the deleted tenant stays writable permanently.

        The tombstone makes the fact READABLE. A peer that missed the event learns the truth the
        next time it looks. A read is self-healing; a missed event is wrong for ever.

        The tombstone keeps only what `keep` allows. That matters: a tenant delete is often an
        ERASURE, so the marker proving they are gone must not itself be a copy of them. The
        identifiers stay; the name, the branding, the config and the domains do not.

        The slug and domain indexes ARE released — the tenant no longer answers on its host, and
        the slug can be taken by someone new. Only the id-keyed marker survives.
        """
        row = {k: v for k, v in tenant.to_dict().items() if k in keep}
        row["id"] = tenant.id                      # the id is the key; it is never optional
        row["status"] = DELETED
        row["deleted_at"] = _now()

        await self._store.put(_TENANTS, tenant.id, row)
        await self._store.delete(_SLUGS, tenant.slug)
        for host in map(normalize_host, tenant.domains):
            await self._store.delete(_DOMAINS, host)
        return Tenant.from_dict(row)
