"""Repositories over the DataStore — tenant-scoped, filtered reads only.

Every list here is a `find()` with a tenant in the `where`, never a `scan_all()`. This is not
style: a scan on a request path grows without bound as the platform succeeds, and `safety:scans`
refuses one. A customer's list is further narrowed to their own `raiser_ref`; an operator's is the
tenant. There is no query that reads across tenants.
"""
from __future__ import annotations

from typing import Any

from app.core.models import Ticket, TicketEvent, new_id, now


class TicketStore:
    collection = "tickets"

    def __init__(self, store) -> None:
        self._s = store

    async def save(self, t: Ticket) -> Ticket:
        await self._s.put(self.collection, t.id, t.to_dict())
        return t

    async def fetch(self, ticket_id: str) -> Ticket | None:
        row = await self._s.get(self.collection, ticket_id)
        return Ticket.from_dict(row) if row else None

    async def find(self, *, tenant: str, raiser_ref: str | None = None,
                   filters: dict[str, Any] | None = None, limit: int = 50,
                   after: str | None = None) -> tuple[list[dict], str | None]:
        """A page of tickets. `tenant` is mandatory (no cross-tenant read); `raiser_ref` narrows to
        one customer's own; `filters` add type/status/assignee/priority. Keyset-paginated."""
        if not tenant:
            raise ValueError("find requires a tenant — an empty scope is every customer")
        where: dict[str, Any] = {"tenant": tenant}
        if raiser_ref:
            where["raiser_ref"] = raiser_ref
        for k in ("type", "status", "assignee", "priority"):
            v = (filters or {}).get(k)
            if v:
                where[k] = v
        return await self._s.find(self.collection, where, limit=limit, after=after,
                                  sort=("created", "desc"))

    async def find_across_tenants(self, *, filters: dict[str, Any] | None = None,
                                  limit: int = 50, after: str | None = None) -> tuple[list[dict], str | None]:
        """A page of tickets ACROSS ALL TENANTS — the platform support sweep. A NON-tenant filter is
        MANDATORY: the datastore forbids find({}) ('a cross-tenant leak one typo away') and scan_all
        is banned on request paths (safety:scans), so an UNFILTERED all-tenants list is impossible by
        design. This is the same non-empty-`where` cross-tenant read billing's /admin/invoices uses.
        Newest-UPDATED first (the active-work order a unified inbox wants); keyset-paginated, and the
        caller caps `limit`. Each row still carries its own `tenant`, so a console can group by shop."""
        where: dict[str, Any] = {}
        for k in ("type", "status", "assignee", "priority"):
            v = (filters or {}).get(k)
            if v:
                where[k] = v
        if not where:
            raise ValueError("a cross-tenant ticket sweep needs at least one filter "
                             "(status/type/priority/assignee) — an unfiltered all-tenants read is a scan")
        return await self._s.find(self.collection, where, limit=limit, after=after,
                                  sort=("updated", "desc"))


class EventStore:
    collection = "ticket_events"

    def __init__(self, store) -> None:
        self._s = store

    async def append(self, *, tenant: str, ticket_id: str, kind: str, actor_ref: str = "",
                     body: str = "", data: dict[str, Any] | None = None) -> TicketEvent:
        ev = TicketEvent(id=new_id(), tenant=tenant, ticket_id=ticket_id, kind=kind,
                         actor_ref=actor_ref, body=body, data=data or {}, created=now())
        await self._s.put(self.collection, ev.id, ev.to_dict())
        return ev

    async def history(self, *, tenant: str, ticket_id: str, limit: int = 200,
                      after: str | None = None) -> tuple[list[dict], str | None]:
        if not tenant or not ticket_id:
            raise ValueError("history requires a tenant and a ticket_id")
        # Ordered by `seq` (monotonic), not `created`: two events in the same microsecond must still
        # read back in the order they were appended, or the history lies about what came first.
        return await self._s.find(self.collection, {"tenant": tenant, "ticket_id": ticket_id},
                                  limit=limit, after=after, sort=("seq", "asc"))
