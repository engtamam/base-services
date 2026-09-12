# =============================================================================
# ticket-service — ticket routes (customer self-service + operator).
# -----------------------------------------------------------------------------
# Tenant ALWAYS comes from the token, never the body. A customer (tickets.self)
# sees and acts on their OWN tickets; an operator (tickets.manage) sees the
# tenant. The workflow (which status may follow which, and who may make the move)
# is enforced in the core service, re-checked here — the client is a map, not the
# authority.
# =============================================================================
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import (InvalidTransition, ReplyForbidden, RequiredFieldMissing,
                                     TicketNotFound, TicketService, TransitionForbidden, UnknownType)

from app.core.pii import subject_ref

router = APIRouter(prefix="/api/v1", tags=["tickets"])


def _actor(principal: Principal, tenant: str) -> str:
    """Pseudonymize the caller before it is written as `actor` into the immutable, hash-chained audit
    log — the raw JWT sub is an email/user-id and must never land in a tamper-evident record
    (ticket-r2-01). Tenant-scoped, matching the actor_ref the ticket event log stores."""
    return subject_ref(get_settings().PII_SALT, tenant, principal.subject)

# Customer self-service, and operator. platform.tickets is the cross-tenant operator override.
_SELF = require_any_capability("tickets.self", "tickets.manage", "platform.tickets")
_MANAGE = require_any_capability("tickets.manage", "platform.tickets")
# platform.tickets ALONE — the platform's own support operators reading a NAMED tenant's tickets
# across the tenancy boundary. Distinct from _MANAGE (which resolves the operator's OWN tenant) and
# from the ShareGrant reads (which require the TENANT's consent for an OUTSIDE party). This platform
# IS the support provider, so a consent-gate would be the wrong model here; every access is audited.
_PLATFORM = require_any_capability("platform.tickets")


def build_tickets(request: Request) -> TicketService:
    from app.core.notifier import NotifierOutbox, TicketNotifier
    from app.core.suggester import TicketSuggester
    c = request.app.state.container
    s = get_settings()
    tickets, events = TicketStore(c.datastore), EventStore(c.datastore)
    outbox = NotifierOutbox(
        c.datastore, c.notification_sender,
        cfg={"retry_base_seconds": s.NOTIFY_RETRY_BASE_SECONDS,
             "retry_max_seconds": s.NOTIFY_RETRY_MAX_SECONDS})
    suggester = TicketSuggester(
        provider=c.suggestion_provider, events=events, tickets=tickets, workflow=c.workflow,
        autotag_min_confidence=s.SUGGEST_AUTOTAG_MIN_CONFIDENCE)
    return TicketService(
        tickets=tickets, events=events, workflow=c.workflow, pii_salt=s.PII_SALT,
        notifier=TicketNotifier(outbox), suggester=suggester)


def _tenant(principal: Principal) -> str:
    if not principal.tenant:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "NO_TENANT")
    return principal.tenant


def _is_operator(principal: Principal) -> bool:
    return "tickets.manage" in principal.caps or "platform.tickets" in principal.caps


class RefIn(BaseModel):
    kind: str = Field(default="", max_length=64)
    id: str = Field(default="", max_length=256)


class OpenIn(BaseModel):
    type: str = Field(min_length=1, max_length=64)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(default="", max_length=20_000)
    ref: RefIn | None = None
    priority: str = Field(default="normal", max_length=16)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ReplyIn(BaseModel):
    body: str = Field(min_length=1, max_length=20_000)


class StatusIn(BaseModel):
    to: str = Field(min_length=1, max_length=64)


class EscalateIn(BaseModel):
    level: str = Field(default="", max_length=64)     # optional severity/tier label
    reason: str = Field(default="", max_length=2000)


class AssignIn(BaseModel):
    assignee: str = Field(min_length=1, max_length=256)


class TagIn(BaseModel):
    tags: list[str] = Field(default_factory=list)


class RateIn(BaseModel):
    score: int = Field(ge=1, le=5)
    comment: str = Field(default="", max_length=2_000)


# ── workflows (so the client can render statuses/transitions) ─────────────────
@router.get("/workflows", dependencies=[Depends(rate_limit)])
async def workflows(request: Request, principal: Principal = Depends(_SELF)) -> dict:
    return {"types": request.app.state.container.workflow.public()}


# ── raise / list / view (customer's own) ──────────────────────────────────────
@router.post("/tickets", status_code=201, dependencies=[Depends(rate_limit)])
async def open_ticket(request: Request, body: OpenIn,
                      principal: Principal = Depends(_SELF)) -> dict:
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.created", actor=_actor(principal, tenant), tenant=tenant, resource="ticket",
        ip=request.client.host if request.client else "", metadata={"type": body.type})
    try:
        t = await build_tickets(request).open(
            tenant=tenant, type=body.type, subject=body.subject, body=body.body,
            raiser=principal.subject, ref=body.ref.model_dump() if body.ref else None,
            priority=body.priority, metadata=body.metadata)
    except UnknownType:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "UNKNOWN_TYPE")
    except RequiredFieldMissing as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            f"MISSING_FIELDS: {','.join(exc.fields)}")
    return t.public()


@router.get("/tickets", dependencies=[Depends(rate_limit)])
async def list_tickets(request: Request, principal: Principal = Depends(_SELF),
                       type: str = Query(default=""), status_: str = Query(default="", alias="status"),
                       limit: int = Query(default=50, ge=1, le=200),
                       cursor: str = Query(default="")) -> dict:
    """A customer's OWN tickets. Operators use /tickets/operator/tickets for the tenant-wide view."""
    tenant = _tenant(principal)
    svc = build_tickets(request)
    rows, nxt = await svc._tickets.find(
        tenant=tenant, raiser_ref=svc.pseudonym(tenant, principal.subject),
        filters={"type": type, "status": status_}, limit=limit, after=cursor or None)
    return {"tickets": rows, "cursor": nxt}


@router.get("/tickets/{ticket_id}", dependencies=[Depends(rate_limit)])
async def get_ticket(request: Request, ticket_id: str,
                     principal: Principal = Depends(_SELF)) -> dict:
    tenant = _tenant(principal)
    svc = build_tickets(request)
    try:
        t = await svc.get(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    # A customer may only see their OWN ticket; an operator sees any in the tenant.
    if not _is_operator(principal) and t.raiser_ref != svc.pseudonym(tenant, principal.subject):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return t.public()


@router.get("/tickets/{ticket_id}/history", dependencies=[Depends(rate_limit)])
async def ticket_history(request: Request, ticket_id: str, principal: Principal = Depends(_SELF),
                         limit: int = Query(default=200, ge=1, le=500),
                         cursor: str = Query(default="")) -> dict:
    tenant = _tenant(principal)
    svc = build_tickets(request)
    try:
        t = await svc.get(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if not _is_operator(principal) and t.raiser_ref != svc.pseudonym(tenant, principal.subject):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    rows, nxt = await svc.history(tenant, ticket_id, limit=limit, after=cursor or None)
    return {"events": rows, "cursor": nxt}


@router.post("/tickets/{ticket_id}/replies", status_code=201, dependencies=[Depends(rate_limit)])
async def reply_ticket(request: Request, ticket_id: str, body: ReplyIn,
                       principal: Principal = Depends(_SELF)) -> dict:
    tenant = _tenant(principal)
    svc = build_tickets(request)
    operator = _is_operator(principal)
    try:
        t = await svc.get(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if not operator and t.raiser_ref != svc.pseudonym(tenant, principal.subject):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    # Per-type reply capability: the workflow declares who may reply to THIS type (default
    # "tickets.self"). It was parsed but never enforced — a non-operator raiser could reply to a type
    # its workflow restricts. An operator (tickets.manage) is always allowed; anyone else must hold
    # the type's declared capability.
    await request.app.state.auditor.record(
        "ticket.replied", actor=_actor(principal, tenant), tenant=tenant, resource=f"ticket:{ticket_id}",
        ip=request.client.host if request.client else "")
    # The per-type reply-capability rule now lives in TicketService.reply (ticket-r2-07): pass the
    # caller's caps and let the core enforce it, so this path and any other are judged identically.
    try:
        t = await svc.reply(tenant=tenant, ticket_id=ticket_id, author=principal.subject,
                            body=body.body, is_operator=operator, caps=set(principal.caps or ()))
    except ReplyForbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")
    return t.public()


@router.post("/tickets/{ticket_id}/rate", dependencies=[Depends(rate_limit)])
async def rate_ticket(request: Request, ticket_id: str, body: RateIn,
                      principal: Principal = Depends(_SELF)) -> dict:
    tenant = _tenant(principal)
    svc = build_tickets(request)
    try:
        t = await svc.get(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    if t.raiser_ref != svc.pseudonym(tenant, principal.subject):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")   # only the raiser rates
    t = await svc.rate(tenant=tenant, ticket_id=ticket_id, rater=principal.subject,
                       score=body.score, comment=body.comment)
    return t.public()


# ── operator: tenant-wide view + workflow moves ───────────────────────────────
@router.get("/tickets/operator/tickets", dependencies=[Depends(rate_limit)])
async def operator_list(request: Request, principal: Principal = Depends(_MANAGE),
                        type: str = Query(default=""), status_: str = Query(default="", alias="status"),
                        assignee: str = Query(default=""), priority: str = Query(default=""),
                        limit: int = Query(default=50, ge=1, le=200),
                        cursor: str = Query(default="")) -> dict:
    tenant = _tenant(principal)
    rows, nxt = await build_tickets(request)._tickets.find(
        tenant=tenant, filters={"type": type, "status": status_, "assignee": assignee,
                                "priority": priority}, limit=limit, after=cursor or None)
    # Embed per-ticket KPIs (first-response / resolution seconds, status) so an operator dashboard can
    # aggregate SLA metrics — average first-response time, count unresolved > 48h — over a read it is
    # already gated for (tickets.manage), with no new privileged surface. It is the SAME pure
    # ticket_kpis the internal single-ticket read uses, applied per row. Each event read is an INDEXED
    # per-ticket query (never a tenant-wide event scan); the page is capped (limit ≤ 200) so the
    # fan-out is bounded, and the reads run concurrently rather than N sequential round trips.
    from app.core.kpis import ticket_kpis
    events = EventStore(request.app.state.container.datastore)
    cap = get_settings().OPERATOR_TICKET_KPI_MAX_EVENTS

    async def _with_kpis(row: dict) -> dict:
        hist, _ = await events.history(tenant=tenant, ticket_id=row.get("id", ""), limit=cap)
        return {**row, "kpis": ticket_kpis(row, hist)}

    enriched = await asyncio.gather(*(_with_kpis(r) for r in rows))
    return {"tickets": enriched, "cursor": nxt}


# ── platform admin: CROSS-TENANT support view (named tenant, no consent, always audited) ───────────
# The platform serves its customers directly, so its support operators must see a given tenant's
# tickets. This reads across the tenancy boundary on PLATFORM authority — NOT on the tenant's consent
# (that is ShareGrant, for OUTSIDE parties). Two hard rules make it safe: platform.tickets ONLY (a
# tenant/operator cap gets 403), and EVERY access is audited (audit is fail-closed in this service, so
# an unrecordable read does not happen). Path is /tickets/admin/tickets (not /tickets/admin) so it can
# never be shadowed by /tickets/{ticket_id}.
@router.get("/tickets/admin/tickets", dependencies=[Depends(rate_limit)])
async def platform_list_tickets(request: Request, principal: Principal = Depends(_PLATFORM),
                                tenant: str = Query(default="", max_length=128),
                                type: str = Query(default=""), status_: str = Query(default="", alias="status"),
                                assignee: str = Query(default=""), priority: str = Query(default=""),
                                limit: int = Query(default=50, ge=1, le=200),
                                cursor: str = Query(default="")) -> dict:
    """Ticket METADATA for a platform support operator. platform.tickets only; audited; full content
    is the per-ticket detail route (a deliberate tenant+id open).

    Two modes on one route:
      * tenant GIVEN  → that one tenant's tickets (created-desc), audited ticket.platform_viewed.
      * tenant OMITTED → a CROSS-TENANT sweep for the unified inbox (updated-desc), audited distinctly
        as ticket.platform_swept. A filter (status/type/priority/assignee) is REQUIRED here — the
        datastore forbids an empty-`where` read and scan_all is banned on request paths, so an
        UNFILTERED all-tenants list is impossible by design (same rule billing's /admin/invoices
        enforces). Each row carries its own `tenant` so the console groups by shop."""
    ip = request.client.host if request.client else ""
    filters = {"type": type, "status": status_, "assignee": assignee, "priority": priority}
    store = build_tickets(request)._tickets

    if tenant:
        await request.app.state.auditor.record(
            "ticket.platform_viewed", actor=_actor(principal, principal.tenant or ""), tenant=principal.tenant or "",
            resource=f"tickets:{tenant}", ip=ip, metadata={"tenant": tenant, "view": "list"})
        rows, nxt = await store.find(tenant=tenant, filters=filters, limit=limit, after=cursor or None)
        return {"tenant": tenant, "tickets": rows, "cursor": nxt}

    # Cross-tenant sweep. A filter is mandatory (see docstring) — refuse BEFORE auditing/reading.
    if not any(filters.values()):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "PLATFORM_SWEEP_NEEDS_A_FILTER")
    await request.app.state.auditor.record(
        "ticket.platform_swept", actor=_actor(principal, principal.tenant or ""), tenant=principal.tenant or "",
        resource="tickets:all-tenants", ip=ip, metadata={"view": "sweep", **{k: v for k, v in filters.items() if v}})
    rows, nxt = await store.find_across_tenants(filters=filters, limit=limit, after=cursor or None)
    return {"tenant": None, "tickets": rows, "cursor": nxt}


@router.get("/tickets/admin/tickets/{ticket_id}", dependencies=[Depends(rate_limit)])
async def platform_get_ticket(request: Request, ticket_id: str,
                              principal: Principal = Depends(_PLATFORM),
                              tenant: str = Query(..., min_length=1, max_length=128),
                              limit: int = Query(default=200, ge=1, le=500),
                              cursor: str = Query(default="")) -> dict:
    """A NAMED tenant's ticket in FULL — subject, body and the reply thread — for a human support
    operator. platform.tickets only; tenant MANDATORY; audited on every access. Crosses the tenancy
    boundary on platform authority; ShareGrant remains the consented path for outside parties."""
    await request.app.state.auditor.record(
        "ticket.platform_viewed", actor=_actor(principal, principal.tenant or ""), tenant=principal.tenant or "",
        resource=f"ticket:{tenant}:{ticket_id}", ip=request.client.host if request.client else "",
        metadata={"tenant": tenant, "view": "detail"})
    svc = build_tickets(request)
    try:
        t = await svc.get(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    events, nxt = await svc.history(tenant, ticket_id, limit=limit, after=cursor or None)
    return {"tenant": tenant, "ticket": t.public(), "events": events, "cursor": nxt}


@router.post("/tickets/{ticket_id}/status", dependencies=[Depends(rate_limit)])
async def transition_ticket(request: Request, ticket_id: str, body: StatusIn,
                            principal: Principal = Depends(_MANAGE)) -> dict:
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.status_changed", actor=_actor(principal, tenant), tenant=tenant,
        resource=f"ticket:{ticket_id}", ip=request.client.host if request.client else "",
        metadata={"to": body.to})
    try:
        t = await build_tickets(request).transition(
            tenant=tenant, ticket_id=ticket_id, to=body.to, actor=principal.subject,
            actor_caps=set(principal.caps), is_operator=True)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    except InvalidTransition:
        raise HTTPException(status.HTTP_409_CONFLICT, "INVALID_TRANSITION")
    except TransitionForbidden:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")
    return t.public()


@router.post("/tickets/{ticket_id}/escalate", dependencies=[Depends(rate_limit)])
async def escalate_ticket(request: Request, ticket_id: str, body: EscalateIn,
                          principal: Principal = Depends(_MANAGE)) -> dict:
    """Mark a ticket as escalated — a first-class support fact recorded in the log, so an SLA/stipend
    KPI can answer 'was it escalated, when, how far' from truth rather than a guess at a status name."""
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.escalated", actor=_actor(principal, tenant), tenant=tenant,
        resource=f"ticket:{ticket_id}", ip=request.client.host if request.client else "",
        metadata={"level": body.level})
    try:
        t = await build_tickets(request).escalate(
            tenant=tenant, ticket_id=ticket_id, actor=principal.subject,
            level=body.level, reason=body.reason, is_operator=True)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return t.public()


@router.post("/tickets/{ticket_id}/assign", dependencies=[Depends(rate_limit)])
async def assign_ticket(request: Request, ticket_id: str, body: AssignIn,
                        principal: Principal = Depends(_MANAGE)) -> dict:
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.assigned", actor=_actor(principal, tenant), tenant=tenant, resource=f"ticket:{ticket_id}",
        ip=request.client.host if request.client else "")
    try:
        t = await build_tickets(request).assign(tenant=tenant, ticket_id=ticket_id,
                                                assignee=body.assignee, actor=principal.subject)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return t.public()


@router.post("/tickets/{ticket_id}/suggest-reply", dependencies=[Depends(rate_limit)])
async def suggest_reply(request: Request, ticket_id: str,
                        principal: Principal = Depends(_MANAGE)) -> dict:
    """An AI-drafted reply for the agent to edit and send. Advisory only — it never sends. Returns
    {"supported": False} when no AI is wired, so the UI hides the control rather than showing a lie."""
    tenant = _tenant(principal)
    try:
        return await build_tickets(request).suggest_reply(tenant, ticket_id)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")


@router.post("/tickets/{ticket_id}/tags", dependencies=[Depends(rate_limit)])
async def tag_ticket(request: Request, ticket_id: str, body: TagIn,
                     principal: Principal = Depends(_MANAGE)) -> dict:
    tenant = _tenant(principal)
    try:
        t = await build_tickets(request).tag(tenant=tenant, ticket_id=ticket_id,
                                             tags=body.tags, actor=principal.subject)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return t.public()
