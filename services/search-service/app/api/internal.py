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
from app.core.query import QueryError
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


class QueryIn(BaseModel):
    """Same shape as the user-tier SearchIn, but the TENANT rides in the body — an internal caller is
    a peer service that already resolved the tenant from ITS OWN caller's token, not a human whose JWT
    carries it."""
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    tenant: str = Field(min_length=1, max_length=128)
    text: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    facets: list[str] = Field(default_factory=list)
    size: int | None = None
    cursor: str = ""
    highlight: bool = True

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
    result = await build_search(request).index(
        index=body.index, doc_id=body.id, tenant=body.tenant, fields=body.fields,
        owner=body.owner, boosts=body.boosts, created=body.created)
    await request.app.state.auditor.record(
        "search.indexed", actor="system", tenant=body.tenant,
        resource=f"{body.index}:{body.id}", ip=request.client.host if request.client else "")
    return result


@router.post("/search/bulk", dependencies=[Depends(require_internal_token)])
async def bulk(request: Request, body: BulkIn) -> dict:
    result = await build_search(request).bulk(body.tenant, body.docs)
    await request.app.state.auditor.record(
        "search.bulk_indexed", actor="system", tenant=body.tenant, resource=body.tenant,
        ip=request.client.host if request.client else "", metadata={"count": str(len(body.docs))})
    return result


@router.post("/search/delete", dependencies=[Depends(require_internal_token)])
async def delete_doc(request: Request, body: DeleteIn) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "search.deleted", actor="", tenant="", resource="search_index",
        ip=request.client.host if request.client else "")
    return await build_search(request).delete(index=body.index, doc_id=body.id, tenant=body.tenant)


@router.get("/search/suggest", dependencies=[Depends(require_internal_token)])
async def internal_suggest(request: Request, index: str, prefix: str, tenant: str) -> dict:
    """Prefix suggest for a PEER service calling on its own caller's behalf.

    The user route (/api/v1/search/suggest) takes the tenant from the caller's JWT — which a peer
    like operations, holding only TOKEN_PUBLIC_KEY, can VERIFY but never MINT (identity-service alone
    keeps the signing key). So it could never present the Bearer token that route requires, and every
    console phone-prefix suggest failed 401, silently, from day one. This is the token-authed twin:
    same SearchService.suggest(), tenant explicit because the peer already resolved it. A READ, so no
    audit row — matching the user route and the internal manifest/health reads."""
    return await build_search(request).suggest(index=index, tenant=tenant, prefix=prefix)


@router.post("/search/query", dependencies=[Depends(require_internal_token)])
async def internal_query(request: Request, body: QueryIn) -> dict:
    """Whole-token search for a PEER service — the internal twin of POST /api/v1/search. Same
    SearchService.search(), tenant in the body. Same 422 on a malformed query as the user route."""
    try:
        return await build_search(request).search(
            index=body.index, tenant=body.tenant, text=body.text, filters=body.filters,
            facets=body.facets, size=body.size, cursor=body.cursor, highlight=body.highlight)
    except QueryError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(e))


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
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = SearchErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
