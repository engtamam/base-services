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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.stores import EventStore, TicketStore
from app.core.ticket_service import (InvalidTransition, RequiredFieldMissing, TicketNotFound,
                                     TicketService, TransitionForbidden, UnknownType)

router = APIRouter(prefix="/api/v1", tags=["tickets"])

# Customer self-service, and operator. platform.tickets is the cross-tenant operator override.
_SELF = require_any_capability("tickets.self", "tickets.manage", "platform.tickets")
_MANAGE = require_any_capability("tickets.manage", "platform.tickets")


def build_tickets(request: Request) -> TicketService:
    c = request.app.state.container
    s = get_settings()
    return TicketService(
        tickets=TicketStore(c.datastore), events=EventStore(c.datastore),
        workflow=c.workflow, pii_salt=s.PII_SALT,
        notifier=getattr(request.app.state, "notifier", None),
        suggester=getattr(request.app.state, "suggester", None))


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
        "ticket.created", actor=principal.subject, tenant=tenant, resource="ticket",
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
    """A customer's OWN tickets. Operators use /operator/tickets for the tenant-wide view."""
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
    await request.app.state.auditor.record(
        "ticket.replied", actor=principal.subject, tenant=tenant, resource=f"ticket:{ticket_id}",
        ip=request.client.host if request.client else "")
    t = await svc.reply(tenant=tenant, ticket_id=ticket_id, author=principal.subject,
                        body=body.body, is_operator=operator)
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
@router.get("/operator/tickets", dependencies=[Depends(rate_limit)])
async def operator_list(request: Request, principal: Principal = Depends(_MANAGE),
                        type: str = Query(default=""), status_: str = Query(default="", alias="status"),
                        assignee: str = Query(default=""), priority: str = Query(default=""),
                        limit: int = Query(default=50, ge=1, le=200),
                        cursor: str = Query(default="")) -> dict:
    tenant = _tenant(principal)
    rows, nxt = await build_tickets(request)._tickets.find(
        tenant=tenant, filters={"type": type, "status": status_, "assignee": assignee,
                                "priority": priority}, limit=limit, after=cursor or None)
    return {"tickets": rows, "cursor": nxt}


@router.post("/tickets/{ticket_id}/status", dependencies=[Depends(rate_limit)])
async def transition_ticket(request: Request, ticket_id: str, body: StatusIn,
                            principal: Principal = Depends(_MANAGE)) -> dict:
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.status_changed", actor=principal.subject, tenant=tenant,
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


@router.post("/tickets/{ticket_id}/assign", dependencies=[Depends(rate_limit)])
async def assign_ticket(request: Request, ticket_id: str, body: AssignIn,
                        principal: Principal = Depends(_MANAGE)) -> dict:
    tenant = _tenant(principal)
    await request.app.state.auditor.record(
        "ticket.assigned", actor=principal.subject, tenant=tenant, resource=f"ticket:{ticket_id}",
        ip=request.client.host if request.client else "")
    try:
        t = await build_tickets(request).assign(tenant=tenant, ticket_id=ticket_id,
                                                assignee=body.assignee, actor=principal.subject)
    except TicketNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return t.public()


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
