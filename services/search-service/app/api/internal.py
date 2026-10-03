# =============================================================================
# search-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations


from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.search import build_search
from app.config import Settings, get_settings
from app.core.erasure import SearchErasure
from app.core.query import QueryError
from app.manifest import build_manifest
from app.core.pii import subject_ref

router = APIRouter(prefix="/internal", tags=["internal"])


class IndexIn(BaseModel):
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    id: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)
    fields: dict[str, Any] = Field(default_factory=dict)
    owner: str = ""
    boosts: dict[str, float] = Field(default_factory=dict)
    created: float = 0.0


class BulkDoc(BaseModel):
    # A bulk doc must pass the SAME bounds as a single IndexIn (search-005): typing docs as raw dicts
    # bypassed the index-pattern / id bounds that index() enforces, so a bulk call could inject an
    # unbounded id or an index name the single path rejects. Tenant rides on BulkIn, not per doc.
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    id: str = Field(min_length=1, max_length=256)
    fields: dict[str, Any] = Field(default_factory=dict)
    owner: str = ""
    boosts: dict[str, float] = Field(default_factory=dict)
    created: float = 0.0


class BulkIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=128)
    docs: list[BulkDoc] = Field(default_factory=list)


class DeleteIn(BaseModel):
    # Same bounds as IndexIn: an internal twin must validate identically to the user model, or an
    # unbounded index/id/tenant slips past the checks its sibling enforces (search-r2-12).
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    id: str = Field(min_length=1, max_length=256)
    tenant: str = Field(min_length=1, max_length=128)


class QueryIn(BaseModel):
    """Same shape as the user-tier SearchIn, but the TENANT rides in the body — an internal caller is
    a peer service that already resolved the tenant from ITS OWN caller's token, not a human whose JWT
    carries it."""
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    tenant: str = Field(min_length=1, max_length=128)
    text: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    facets: list[str] = Field(default_factory=list)
    size: int | None = Field(default=None, ge=1, le=1000)
    cursor: str = ""
    highlight: bool = True

_HEADER = "X-Internal-Token"
_ASSERTION_HEADER = "X-Internal-Assertion"


async def require_internal_token(
    request: Request,
    x_internal_token: str | None = Header(default=None, alias=_HEADER),
    x_internal_assertion: str | None = Header(default=None, alias=_ASSERTION_HEADER),
    settings: Settings = Depends(get_settings),
) -> None:
    """Authenticate the internal caller, then authorise it for THIS route. The name is unchanged so
    every existing `Depends(require_internal_token)` keeps working, but it now does two things:

      1. IDENTITY — a config-resolved (cached) driver verifies the caller. `shared` (default) is
         the timing-safe flat-token compare (today's behaviour, unnamed peer); `static`/`jwks` verify a
         per-caller RS256 assertion. Any failure is one generic 401 — never which check failed.
      2. POLICY — the per-route allow-list decides whether that caller may reach `request.url.path`. A
         route with no policy admits any authenticated caller (default-open), so identity alone never
         breaks a path; a named allow-list yields 403 for a caller not on it.

    The verifier + policy are resolved from config (cached), not the container, so this seam is
    identical in every service."""
    from app.core.internal_caller_factory import resolve_internal_caller, resolve_internal_policy
    from app.ports.internal_caller import InternalCallerError

    audience = settings.INTERNAL_CALLER_AUDIENCE or settings.SERVICE_NAME
    try:
        identity = resolve_internal_caller(settings).verify(
            token=x_internal_token or "", assertion=x_internal_assertion or "", audience=audience)
    except InternalCallerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "UNAUTHORIZED")
    if not resolve_internal_policy(settings).permits(request.url.path, identity.caller):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


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
    result = await build_search(request).bulk(body.tenant, [d.model_dump() for d in body.docs])
    await request.app.state.auditor.record(
        "search.bulk_indexed", actor="system", tenant=body.tenant, resource=body.tenant,
        ip=request.client.host if request.client else "", metadata={"count": str(len(body.docs))})
    return result


@router.post("/search/delete", dependencies=[Depends(require_internal_token)])
async def delete_doc(request: Request, body: DeleteIn) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    # Attribute the irreversible delete (search-002): record the tenant and the exact document
    # (index:id), not a constant "search_index" with empty tenant — otherwise the critical audit that
    # exists because "nothing here can show what was removed afterwards" shows nothing either.
    await request.app.state.auditor.record(
        "search.deleted", actor="service:internal", tenant=body.tenant,
        resource=f"{body.index}:{body.id}",
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
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{subject_ref(get_settings().PII_SALT, body.tenant, body.subject)}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    # The index engine so erase scrubs the doc row AND its postings AND meta (not just a raw row that
    # left the person's terms searchable) — and enforces tenant on each delete.
    eraser = SearchErasure(store=c.datastore, index=build_search(request)._index, pii_salt=s.PII_SALT,
                           cache=c.cache)
    return await eraser.erase(body.tenant, body.subject)
