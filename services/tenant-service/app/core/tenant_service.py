"""Core tenant logic — provisioning, resolution, lifecycle (with events), export.

Framework-free; depends only on ports (repository over DataStore, IsolationStrategy,
DomainResolver, EventBus). Lifecycle changes emit domain events so peers can react
(e.g. purge a deleted tenant's data) without any code coupling.
"""
from __future__ import annotations

import datetime as _dt
import re
from typing import Any

from app.core.models import DELETED, SUSPENDED, Tenant, new_id
from app.ports.errors import ConfigError

# How a delete behaves. The PLATFORM decides; we enforce. `tombstone` is the default because the
# event bus is at-most-once and a purge makes the deletion unlearnable to anyone who missed it.
TOMBSTONE, PURGE = "tombstone", "purge"
DELETE_MODES = (TOMBSTONE, PURGE)
from app.core.tenant_repository import TenantRepository
from app.ports.event_bus import Event

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*[a-z0-9]$")


class InvalidSlug(Exception):
    pass


class InvalidName(Exception):
    pass


# A display NAME is shown on the account screen, invoices and support — so it must not carry control
# characters (which corrupt a document or a terminal) and must not be blank once trimmed. It is
# otherwise free text: the point of the rename is to ALLOW correcting a garbage autofill, not to have
# a validator clever enough to have rejected `Cacad$453453`, which is a perfectly well-formed string.
_NAME_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class TenantService:
    def __init__(self, *, repo: TenantRepository, isolation, resolver, event_bus,
                 default_isolation: str, default_locale: str, default_timezone: str,
                 default_calendar: str, slug_min: int, slug_max: int,
                 delete_mode: str = "tombstone",
                 tombstone_fields: tuple[str, ...] = ()) -> None:
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
        if delete_mode not in DELETE_MODES:
            raise ConfigError(
                f"unknown TENANT_DELETE_MODE {delete_mode!r} — allowed: {', '.join(DELETE_MODES)}")
        self._delete_mode = delete_mode
        self._tombstone_fields = tuple(tombstone_fields)

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

    async def rename(self, tenant: Tenant, name: str) -> Tenant:
        """Change the tenant's NAME — the record-of-truth name captured once at signup and, until now,
        unchangeable. A browser autofilled a password into that field on a real pilot shop; the garbage
        was then the first thing its owner read on his own account, and propagated to anything the
        platform renders (invoices, exports, admin). The slug and identity are untouched — only the
        human-facing name moves. Raises InvalidName on a blank or control-character value."""
        cleaned = (name or "").strip()
        if not cleaned or len(cleaned) > 256 or _NAME_CONTROL_RE.search(cleaned):
            raise InvalidName(f"invalid tenant name: {name!r}")
        tenant.name = cleaned
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
        # Reactivating a tenant that was closing is how the owner takes it back within the grace
        # window — clear the schedule so the sweep does not erase a tenant that is live again.
        tenant.config.pop("_close", None)
        await self._repo.update(tenant)
        await self._emit("tenant.reactivated", tenant)
        return tenant

    async def close(self, tenant: Tenant, *, grace_days: int, actor: str = "") -> dict:
        """Owner self-close: suspend now, erase after grace — recoverable until then.

        The tenant is SUSPENDED immediately (access stops), and a grace deadline is written on it.
        Reactivating before the deadline takes the close back; the sweep erases it after. With
        grace_days <= 0 there is no recovery window and it is erased at once. Idempotent: closing an
        already-closed/closing tenant returns its existing schedule rather than resetting the clock.
        """
        if tenant.status == DELETED:
            return {"status": DELETED, "tenant_id": tenant.id, "idempotent": True}
        if grace_days <= 0:
            res = await self.delete(tenant)
            res["closed"] = True
            return res
        existing = tenant.config.get("_close") or {}
        if tenant.status == SUSPENDED and existing.get("grace_until"):
            return {"status": SUSPENDED, "tenant_id": tenant.id,
                    "grace_until": existing["grace_until"], "closed": True, "idempotent": True}
        now_dt = _dt.datetime.now(_dt.timezone.utc)
        grace_until = (now_dt + _dt.timedelta(days=grace_days)).isoformat()
        tenant.status = SUSPENDED
        tenant.config["_close"] = {"scheduled_at": now_dt.isoformat(),
                                   "grace_until": grace_until, "closed_by": actor}
        tenant.touch()
        await self._repo.update(tenant)
        await self._emit("tenant.close_scheduled", tenant)
        return {"status": SUSPENDED, "tenant_id": tenant.id, "grace_until": grace_until,
                "closed": True}

    async def close_sweep(self, *, now_iso: str, limit: int = 200) -> dict:
        """Erase tenants whose close grace has elapsed. Driven by the platform scheduler.

        A bounded read (never a platform-wide scan), so a deployment with more closing tenants than
        `limit` in one window simply finishes them on the next pass rather than doing an unbounded
        read on a schedule."""
        rows = await self._repo.list(limit)
        erased = 0
        for r in rows:
            sched = (r.get("config") or {}).get("_close") or {}
            grace_until = sched.get("grace_until")
            if r.get("status") == SUSPENDED and grace_until and grace_until <= now_iso:
                await self.delete(Tenant.from_dict(r))
                erased += 1
        return {"scanned": len(rows), "erased": erased}

    async def delete(self, tenant: Tenant) -> dict:
        """Delete the tenant, and leave something a peer can READ that says so.

        `tenant.deleted` is published AT MOST ONCE — the bus has no outbox, no retry and no replay
        (see the EventBus port). A peer that is restarting when it fires never hears about it. If
        the row is also purged, there is then nothing left to ask: GET /internal/tenants/{id} 404s
        for ever, peers fail OPEN on 404 (an unknown tenant is usually an onboarding gap, not a
        punishment), and a deleted tenant stays writable permanently.

        A read is self-healing. A missed event is wrong for ever. So the default is a TOMBSTONE:
        the tenant is gone, and the fact that it is gone is still readable.

        `purge` remains available for the deployment that genuinely wants the row destroyed — but
        it is a choice made with eyes open, not the default nobody noticed.
        """
        if self._delete_mode == PURGE:
            await self._repo.delete(tenant)
            await self._emit("tenant.deleted", tenant)
            return {"status": DELETED, "tenant_id": tenant.id, "purged": True}

        stone = await self._repo.tombstone(tenant, keep=self._tombstone_fields)
        await self._emit("tenant.deleted", tenant)   # a HINT that saves peers a round trip
        return {"status": DELETED, "tenant_id": stone.id, "purged": False,
                "deleted_at": stone.deleted_at}

    # ── offboarding / scope ────────────────────────────────────────────────────
    async def export(self, tenant: Tenant) -> dict:
        return {"tenant": tenant.to_dict(), "scope": self.scope(tenant)}

    def scope(self, tenant: Tenant) -> dict[str, str]:
        return self._isolation.scope(tenant.id, tenant.slug)
