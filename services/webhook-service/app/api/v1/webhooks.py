# =============================================================================
# webhook-service — outbound endpoints, deliveries, DLQ/replay, health
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.inbound_service import InboundService
from app.core.ssrf import SsrfBlocked
from app.core.stores import DeliveryStore, EndpointStore, EventStore, InboundStore
from app.core.webhook_service import WebhookService

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

_ADMIN = require_any_capability("platform.webhooks", "webhooks.manage")
_READ = require_any_capability("platform.webhooks", "webhooks.manage", "webhooks.read")


def _config() -> dict[str, Any]:
    s = get_settings()
    return {"require_https": s.WEBHOOK_REQUIRE_HTTPS, "allow_private_ips": s.WEBHOOK_ALLOW_PRIVATE_IPS,
            "allowed_hosts": s.webhook_allowed_hosts, "denied_hosts": s.webhook_denied_hosts,
            "timeout_seconds": s.DELIVERY_TIMEOUT_SECONDS,
            "retry_base_seconds": s.RETRY_BASE_SECONDS, "retry_max_seconds": s.RETRY_MAX_SECONDS,
            "retry_max_attempts": s.RETRY_MAX_ATTEMPTS, "retry_jitter": s.RETRY_JITTER,
            "circuit_threshold": s.CIRCUIT_FAILURE_THRESHOLD,
            "circuit_reset_seconds": s.CIRCUIT_RESET_SECONDS,
            "max_payload_bytes": s.MAX_PAYLOAD_BYTES, "max_response_bytes": s.MAX_RESPONSE_BYTES,
            "endpoint_concurrency": s.ENDPOINT_CONCURRENCY, "list_limit": s.WEBHOOK_LIST_LIMIT}


def build_webhooks(request: Request) -> WebhookService:
    c = request.app.state.container
    s = get_settings()
    return WebhookService(
        endpoints=EndpointStore(c.datastore, c.cache, s.SUBSCRIPTION_CACHE_TTL_SECONDS),
        events=EventStore(c.datastore), deliveries=DeliveryStore(c.datastore),
        transport=c.webhook_transport, signer=c.signer, config=_config(), event_bus=c.event_bus)


async def build_inbound(request: Request) -> InboundService:
    # ASYNC because the accepted secrets are RESOLVED, not captured: an operator may have entered
    # or rotated one since boot. The pool answers from a short-lived cache rather than reading the
    # credential store on every inbound event.
    c = request.app.state.container
    s = get_settings()
    return InboundService(store=InboundStore(c.datastore), signer=c.signer,
                          secrets=await request.app.state.inbound_pool.get(),
                          tolerance_seconds=s.INBOUND_TIMESTAMP_TOLERANCE_SECONDS,
                          max_payload_bytes=s.MAX_PAYLOAD_BYTES,
                          dedupe_ttl_seconds=s.INBOUND_DEDUPE_TTL_SECONDS, event_bus=c.event_bus)


def _tenant(p: Principal, body_tenant: str | None = None) -> str:
    if "platform.webhooks" in p.caps:
        return body_tenant or p.tenant or "global"
    return p.tenant or "__none__"


async def _owned(svc: WebhookService, endpoint_id: str, p: Principal):
    ep = await svc._endpoints.get(endpoint_id)
    if ep is None or ("platform.webhooks" not in p.caps and ep.tenant != (p.tenant or "__none__")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return ep


class EndpointIn(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    event_types: list[str] = Field(default_factory=lambda: ["*"])
    secret: str = ""
    description: str = ""
    tenant: str | None = None


class EndpointPatch(BaseModel):
    url: str | None = None
    event_types: list[str] | None = None
    enabled: bool | None = None
    description: str | None = None


class DispatchIn(BaseModel):
    tenant: str = Field(min_length=1)
    type: str = Field(min_length=1, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)


# ── endpoints ─────────────────────────────────────────────────────────────────
@router.post("/endpoints", status_code=201)
async def register(request: Request, body: EndpointIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "webhook.endpoint_created", actor=principal.subject, tenant=principal.tenant, resource="endpoint",
        ip=request.client.host if request.client else "")
    try:
        ep = await build_webhooks(request).register(
            tenant=_tenant(principal, body.tenant), url=body.url,
            event_types=body.event_types, secret=body.secret, description=body.description)
    except SsrfBlocked as e:
        raise HTTPException(400, f"URL_REJECTED: {e}")
    out = ep.public()
    out["secret"] = ep.secrets[0]        # shown ONCE, at creation
    return out


@router.get("/endpoints", dependencies=[Depends(_READ)])
async def list_endpoints(request: Request, principal: Principal = Depends(_READ)) -> dict:
    rows = await build_webhooks(request)._endpoints.list(_tenant(principal), get_settings().WEBHOOK_LIST_LIMIT)
    from app.core.models import Endpoint
    return {"endpoints": [Endpoint.from_dict(r).public() for r in rows]}


@router.patch("/endpoints/{endpoint_id}")
async def update_endpoint(request: Request, endpoint_id: str, body: EndpointPatch,
                          principal: Principal = Depends(_ADMIN)) -> dict:
    svc = build_webhooks(request)
    await _owned(svc, endpoint_id, principal)
    try:
        ep = await svc.update(endpoint_id, **body.model_dump())
    except SsrfBlocked as e:
        raise HTTPException(400, f"URL_REJECTED: {e}")
    return ep.public()


@router.post("/endpoints/{endpoint_id}/rotate-secret")
async def rotate(request: Request, endpoint_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "webhook.secret_rotated", actor=principal.subject, tenant=principal.tenant, resource=f"endpoint:{endpoint_id}",
        ip=request.client.host if request.client else "")
    svc = build_webhooks(request)
    await _owned(svc, endpoint_id, principal)
    ep, fresh = await svc.rotate_secret(endpoint_id)
    return {"endpoint_id": ep.id, "secret": fresh, "previous_still_accepted": len(ep.secrets) > 1}


@router.delete("/endpoints/{endpoint_id}")
async def delete_endpoint(request: Request, endpoint_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "webhook.endpoint_deleted", actor=principal.subject, tenant=principal.tenant, resource=f"endpoint:{endpoint_id}",
        ip=request.client.host if request.client else "")
    svc = build_webhooks(request)
    await _owned(svc, endpoint_id, principal)
    return {"deleted": await svc.delete(endpoint_id)}


@router.get("/endpoints/{endpoint_id}/health", dependencies=[Depends(_READ)])
async def endpoint_health(request: Request, endpoint_id: str, principal: Principal = Depends(_READ)) -> dict:
    svc = build_webhooks(request)
    await _owned(svc, endpoint_id, principal)
    return await svc.health(endpoint_id)


# ── deliveries / DLQ / replay ──────────────────────────────────────────────────
@router.get("/deliveries", dependencies=[Depends(_READ)])
async def list_deliveries(request: Request, status_filter: str | None = None,
                          principal: Principal = Depends(_READ)) -> dict:
    svc = build_webhooks(request)
    return {"deliveries": await svc._deliveries.list(_tenant(principal), status=status_filter,
                                                     limit=get_settings().WEBHOOK_LIST_LIMIT)}


@router.get("/dead-letters", dependencies=[Depends(_READ)])
async def dead_letters(request: Request, principal: Principal = Depends(_READ)) -> dict:
    svc = build_webhooks(request)
    return {"dead_letters": await svc._deliveries.dead_letters(_tenant(principal),
                                                               get_settings().WEBHOOK_LIST_LIMIT)}


@router.post("/deliveries/{delivery_id}/replay", dependencies=[Depends(_ADMIN)])
async def replay_delivery(request: Request, delivery_id: str, principal: Principal = Depends(_ADMIN)) -> dict:
    svc = build_webhooks(request)
    d = await svc._deliveries.get(delivery_id)
    if d is None or ("platform.webhooks" not in principal.caps and d.tenant != (principal.tenant or "__none__")):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return await svc.replay(delivery_id)


@router.post("/events/{event_id}/replay", dependencies=[Depends(_ADMIN)])
async def replay_event(request: Request, event_id: str) -> dict:
    return await build_webhooks(request).replay_event(event_id)


@router.post("/dispatch", status_code=202, dependencies=[Depends(_ADMIN)])
async def dispatch(request: Request, body: DispatchIn, principal: Principal = Depends(_ADMIN)) -> dict:
    try:
        return await build_webhooks(request).dispatch(
            tenant=_tenant(principal, body.tenant), event_type=body.type, payload=body.payload)
    except ValueError as e:
        raise HTTPException(413, str(e))


@router.post("/sweep", dependencies=[Depends(_ADMIN)])
async def sweep(request: Request) -> dict:
    return await build_webhooks(request).sweep()
