"""TenantRepository over the generic DataStore port (id + slug + domain indexes)."""
from __future__ import annotations

from app.adapters.resolver.resolvers import normalize_host
from app.core.models import DELETED, STATUSES, SUSPENDED, Tenant, now as _now
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
        # Create the id-keyed row via the insert-if-absent CAS (expected_version=0), NOT put(), so it
        # lands carrying version=1. This is not cosmetic: every lifecycle/config write goes through the
        # version-pinned cas_update() (see _apply), and the postgres adapter's expected_version=0
        # branch is a strict INSERT ... ON CONFLICT DO NOTHING. A row written by put() has no version,
        # so the first cas_update's cas(id, 0, ...) is read as insert-if-absent against an
        # already-present row -> DO NOTHING -> False, and _apply exhausts its retries as
        # ConcurrentModification on the very first suspend/activate/config change (the memory adapter
        # used to treat versionless as 0 and let it through, so every unit test passed). The
        # _SLUGS/_DOMAINS uniqueness indexes above already create this way. tenant.id is a fresh
        # new_id(), so the insert always wins.
        await self._store.cas(_TENANTS, tenant.id, 0, tenant.to_dict())
        tenant.version = 1
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

    async def heal_version(self, tenant_id: str) -> Tenant | None:
        """Repair a legacy row stuck at version 0 so its version-pinned cas_update can win.

        cas() reserves expected_version=0 for insert-if-absent (both adapters, by contract), so a row
        that carries version 0 — one written by the pre-fix put() create path, before create() stamped
        version=1 — can never be CAS-updated: every _apply reads version 0, cas(id, 0, ...) is
        insert-if-absent against an already-present row -> DO NOTHING -> False, and all retries exhaust
        as ConcurrentModification. Stamp such a row to version 1 with a CONTENT-PRESERVING put (no
        mutation), after which the normal version-pinned path works. Idempotent and race-safe: a
        version-0 row is unreachable by cas, so any concurrent writer is also in this branch writing
        identical bytes; a row already at version>=1 is returned unchanged."""
        row = await self._store.get(_TENANTS, tenant_id)
        if row is None:
            return None
        if int(row.get("version") or 0) == 0:
            row = {**row, "version": 1}
            # atomic: ok — content-preserving version stamp (0->1) on a row cas structurally cannot reach; a version-0 row is unreachable by cas, so any concurrent healer writes byte-identical content, no update is lost
            await self._store.put(_TENANTS, tenant_id, row)
        return Tenant.from_dict(row)

    async def cas_update(self, tenant: Tenant, expected_version: int,
                         *, old_domains: list[str] | None = None) -> bool:
        """Version-checked tenant-row write. True if it won, False if another writer changed the row
        first (the caller re-loads and re-applies). Every lifecycle/config mutation goes through this
        so a concurrent write cannot silently revert a status transition (tenant-r2-01). Domain
        re-indexing stays a plain put — the id-keyed row is the lifecycle authority."""
        tenant.touch()
        for host in map(normalize_host, old_domains or []):
            if host not in map(normalize_host, tenant.domains):
                await self._store.delete(_DOMAINS, host)
        for host in map(normalize_host, tenant.domains):
            await self._store.put(_DOMAINS, host, {"slug": tenant.slug})
        if await self._store.cas(_TENANTS, tenant.id, int(expected_version), tenant.to_dict()):
            tenant.version = int(expected_version) + 1
            return True
        return False

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: this IS the tenant catalogue. There is no tenant to scope it by, and it is
        # reachable only by a platform operator. Bounded by the caller's limit.
        return await self._store.scan_all(_TENANTS, limit)

    async def list_page(self, limit: int = 100, after: str | None = None):
        """tenant-007: a CURSOR-paginated page of the catalogue, so an operator can walk EVERY tenant
        instead of silently seeing only the first TENANT_LIST_LIMIT. Filtered on the full status set
        (find needs a non-empty where; every tenant carries one of STATUSES) and ordered by created_at."""
        return await self._store.find(_TENANTS, {"status": list(STATUSES)}, limit=limit, after=after,
                                      sort=("created_at", "asc"))

    async def iter_suspended(self, limit: int = 200, after: str | None = None):
        """A page of SUSPENDED tenants (the close-sweep's candidate set) — pushed into the store as a
        filter so the sweep reaches EVERY closing tenant over its pages, not just the first N rows of
        the whole catalogue."""
        return await self._store.find(_TENANTS, {"status": SUSPENDED}, limit=limit, after=after,
                                      sort=("created_at", "asc"))   # model field is created_at, not created

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
        # BUMP the version so a config/lifecycle write that loaded the pre-delete row loses its CAS and
        # re-reads the tombstone (then refuses) instead of clobbering it back to active (tenant-r2-01/03).
        row["version"] = int(tenant.version) + 1

        await self._store.put(_TENANTS, tenant.id, row)
        await self._store.delete(_SLUGS, tenant.slug)
        for host in map(normalize_host, tenant.domains):
            await self._store.delete(_DOMAINS, host)
        return Tenant.from_dict(row)
