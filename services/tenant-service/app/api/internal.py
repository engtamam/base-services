# =============================================================================
# tenant-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from typing import Any

from app.api.v1.tenants import build_service
from app.config import Settings, get_settings
from app.core.erasure import TenantErasure
from app.core.tenant_repository import DomainTaken, SlugTaken
from app.core.tenant_service import InvalidSlug
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])

_HEADER = "X-Internal-Token"


async def require_internal_token(
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Deny unless the caller presents the exact internal token (timing-safe)."""
    expected = settings.INTERNAL_SERVICE_TOKEN
    provided = x_internal_token or ""
    # hmac.compare_digest is constant-time and length-safe.
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="UNAUTHORIZED"
        )


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.get("/tenants/resolve", dependencies=[Depends(require_internal_token)])
async def internal_resolve(request: Request, host: str = Query(..., max_length=253)) -> dict:
    """host -> tenant (for peers routing by domain). 404 if unresolved."""
    tenant = await build_service(request).resolve_by_host(host)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return _internal_view(tenant, build_service(request))


def _internal_view(tenant, svc) -> dict:
    """What a peer may read. The FIELD LIST is config (TENANT_INTERNAL_FIELDS) — a platform that
    must not leak branding to peers removes it there, without forking this route."""
    allowed = get_settings().internal_fields
    row = tenant.to_dict()
    out = {k: v for k, v in row.items() if k in allowed}
    out["id"] = tenant.id                       # never optional: it is what was asked for
    out["status"] = tenant.status               # never optional: it is the answer
    out["isolation"] = svc.scope(tenant)
    return out


@router.get("/tenants/{tenant_id}", dependencies=[Depends(require_internal_token)])
async def internal_get(request: Request, tenant_id: str) -> dict:
    """Tenant config + scope for peers (data scoping).

    A DELETED tenant answers 200 with `status: "deleted"` and `deleted_at` — it does NOT 404.

    That is the whole point of the tombstone. `tenant.deleted` is published at most once, so a peer
    that was restarting when it fired never heard it. If this 404'd, that peer would fail OPEN (an
    unknown tenant is usually an onboarding gap, not a punishment) and keep accepting writes to a
    company that no longer exists — for ever, because nothing would ever tell it again. Answering
    with the tombstone makes the deletion something a peer LEARNS BY LOOKING. A read is
    self-healing; a missed event is wrong for ever.

    Peers: treat `status == "deleted"` as a hard stop. It is not an onboarding gap.

    `branding` is included so a render path (a PDF carrying the customer's logo) can read it with
    the internal token alone, instead of minting a token with a cross-tenant admin capability just
    to fetch a logo. Least privilege: the smaller capability must be enough for the smaller job.
    """
    svc = build_service(request)
    tenant = await svc.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return _internal_view(tenant, svc)


@router.get("/manifest", dependencies=[Depends(require_internal_token)])
async def internal_manifest(
    request: Request, settings: Settings = Depends(get_settings)
) -> dict:
    container = getattr(request.app.state, "container", None)
    ports = container.describe() if container is not None else []
    manifest = build_manifest(settings, ports=ports)
    guard = getattr(request.app.state, "cwe_guard", None)
    if guard is not None:
        manifest["security"] = {"cwe_guard": guard.report()}
    return manifest


class EraseIn(BaseModel):
    tenant: str
    subject: str


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Answer the erasure contract so a DSAR can prove it asked this service."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = TenantErasure(store=c.datastore, pii_salt=s.PII_SALT, max_scan=s.ERASURE_MAX_SCAN)
    return await eraser.erase(body.tenant, body.subject)


@router.post("/tenants/close-sweep", dependencies=[Depends(require_internal_token)])
async def close_sweep(request: Request) -> dict:
    """Erase tenants whose owner-close grace window has elapsed. Driven by the platform scheduler,
    not a thread in here, so a restart cannot lose the only thing tracking a due closure."""
    import datetime as _dt
    now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return await build_service(request).close_sweep(now_iso=now_iso)


class ProvisionTenantIn(BaseModel):
    slug: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=256)
    domains: list[str] = Field(default_factory=list)
    isolation_mode: str | None = None
    config: dict[str, Any] = Field(default_factory=dict)
    # The provisioning envelope onboarding sends (tenant/subject/idempotency_key) is ignored here —
    # pydantic drops the extras; slug uniqueness IS the idempotency (a repeat provision -> 409).


@router.post("/tenants", status_code=201, dependencies=[Depends(require_internal_token)])
async def internal_create(request: Request, body: ProvisionTenantIn) -> dict:
    """Service-to-service tenant creation, for the onboarding/provisioning saga.

    Provisioning is service-to-service by nature, so it is guarded by the shared internal token like
    the rest of this tier — NOT by the user-tier POST /api/v1/tenants, which requires a Bearer JWT
    carrying platform.tenants. Minting such a platform-capable token inside onboarding just to call a
    user route would put a very powerful credential in a service that otherwise needs none. Returns
    `id` (the provisioner's undo reference); a repeat slug is 409 (the caller treats it as already-ours)."""
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
    # tenant.created is a DECLARED auditable action. Internal seam: no user principal, so actor is
    # empty and the source IP names the peer.
    await request.app.state.auditor.record(
        "tenant.created", actor="", tenant=t.id, resource=f"tenant:{t.id}",
        ip=request.client.host if request.client else "")
    return t.public()


@router.delete("/tenants/{tenant_id}", dependencies=[Depends(require_internal_token)])
async def internal_delete(request: Request, tenant_id: str) -> dict:
    """The provisioner's UNDO: compensate a created tenant. 404 when already gone (the saga treats
    that as success on a resumed rollback). Audited write-ahead — a delete the log cannot take must
    not happen — matching the user-tier delete."""
    svc = build_service(request)
    tenant = await svc.get(tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    await request.app.state.auditor.record(
        "tenant.deleted", actor="", tenant=tenant_id, resource=f"tenant:{tenant_id}",
        ip=request.client.host if request.client else "")
    return await svc.delete(tenant)
