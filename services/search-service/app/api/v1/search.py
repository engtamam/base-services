# =============================================================================
# search-service — user search/suggest + admin index management
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.query import QueryError
from app.core.search_service import SearchService

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_ADMIN = require_any_capability("platform.search", "search.manage")
_READ = require_any_capability("platform.search", "search.manage", "search.read")
# The index CATALOGUE (names + aggregate doc counts) is GLOBAL/cross-tenant in this shared-index
# engine — meta is per-index, not per-(tenant,index). search.manage is a TENANT-scoped operator cap,
# so it must NOT read global counts (it would leak other tenants' index names + volumes). Restrict the
# catalogue/stats reads to the PLATFORM capability only. (Per-tenant stats need a per-tenant meta row.)
_PLATFORM = require_any_capability("platform.search")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"arabic": s.ARABIC_NORMALIZE, "stopwords": s.STOPWORDS_ENABLED,
            "cache_ttl": s.SEARCH_CACHE_TTL_SECONDS, "default_size": s.DEFAULT_PAGE_SIZE,
            "max_size": s.MAX_PAGE_SIZE, "max_query_len": s.MAX_QUERY_LEN,
            "max_query_terms": s.MAX_QUERY_TERMS, "typo_tolerance": s.TYPO_TOLERANCE,
            "suggest_max": s.SUGGEST_MAX, "list_limit": s.SEARCH_LIST_LIMIT,
            "max_candidates": s.SEARCH_MAX_CANDIDATES}


def build_search(request: Request) -> SearchService:
    c = request.app.state.container
    return SearchService(index=c.search_index, cache=c.cache, config=_cfg(), event_bus=c.event_bus)


def _tenant(p: Principal) -> str:
    # HARD isolation: the search tenant ALWAYS comes from the caller's identity, never the body.
    # A principal with no resolved tenant is REFUSED, not collapsed into a shared "__none__" scope
    # where every tenantless caller would read and write one another's documents (search-r2-10).
    if not p.tenant:
        raise HTTPException(status_code=403, detail="NO_TENANT_SCOPE")
    return p.tenant


class SearchIn(BaseModel):
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    text: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    facets: list[str] = Field(default_factory=list)
    size: int | None = None
    cursor: str = ""
    highlight: bool = True


@router.post("", dependencies=[Depends(_READ), Depends(rate_limit)])
async def search(request: Request, body: SearchIn, principal: Principal = Depends(_READ)) -> dict:
    try:
        return await build_search(request).search(
            index=body.index, tenant=_tenant(principal), text=body.text, filters=body.filters,
            facets=body.facets, size=body.size, cursor=body.cursor, highlight=body.highlight)
    except QueryError as e:
        raise HTTPException(422, str(e))


@router.get("/suggest", dependencies=[Depends(_READ), Depends(rate_limit)])
async def suggest(request: Request, index: str, prefix: str, principal: Principal = Depends(_READ)) -> dict:
    return await build_search(request).suggest(index=index, tenant=_tenant(principal), prefix=prefix)


@router.get("/indexes", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def list_indexes(request: Request) -> dict:
    """The index catalogue — which indexes exist and how many documents each holds. GLOBAL/cross-tenant
    (meta is per-index, not per-tenant), so PLATFORM-only: a tenant operator must not learn other
    tenants' index names or volumes. Read-only; the panel never mutates an index (indexing is
    internal-token only)."""
    return {"indexes": await build_search(request).list_indexes()}


@router.get("/indexes/{index}/stats", dependencies=[Depends(_PLATFORM), Depends(rate_limit)])
async def index_stats(request: Request, index: str) -> dict:
    return await build_search(request).stats(index=index)
