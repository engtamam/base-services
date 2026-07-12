# =============================================================================
# search-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.search import build_search
from app.config import Settings, get_settings
from app.core.erasure import SearchErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class IndexIn(BaseModel):
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    id: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    fields: dict[str, Any] = Field(default_factory=dict)
    owner: str = ""
    boosts: dict[str, float] = Field(default_factory=dict)
    created: float = 0.0


class BulkIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    docs: list[dict[str, Any]] = Field(default_factory=list)


class DeleteIn(BaseModel):
    index: str
    id: str
    tenant: str

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


@router.post("/search/index", status_code=201, dependencies=[Depends(require_internal_token)])
async def index_doc(request: Request, body: IndexIn) -> dict:
    """Any service indexes an entity it owns. Tenant is explicit here (service-to-service)."""
    return await build_search(request).index(
        index=body.index, doc_id=body.id, tenant=body.tenant, fields=body.fields,
        owner=body.owner, boosts=body.boosts, created=body.created)


@router.post("/search/bulk", dependencies=[Depends(require_internal_token)])
async def bulk(request: Request, body: BulkIn) -> dict:
    return await build_search(request).bulk(body.tenant, body.docs)


@router.post("/search/delete", dependencies=[Depends(require_internal_token)])
async def delete_doc(request: Request, body: DeleteIn) -> dict:
    return await build_search(request).delete(index=body.index, doc_id=body.id, tenant=body.tenant)


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
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent —
    a DSAR retries, so erasing an already-erased subject must not fail.
    """
    c = request.app.state.container
    s = get_settings()
    eraser = SearchErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
