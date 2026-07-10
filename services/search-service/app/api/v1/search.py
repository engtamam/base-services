# =============================================================================
# search-service — user search/suggest + admin index management
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.query import QueryError
from app.core.search_service import SearchService

router = APIRouter(prefix="/api/v1/search", tags=["search"])

_ADMIN = require_any_capability("platform.search", "search.manage")
_READ = require_any_capability("platform.search", "search.manage", "search.read")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"arabic": s.ARABIC_NORMALIZE, "stopwords": s.STOPWORDS_ENABLED,
            "cache_ttl": s.SEARCH_CACHE_TTL_SECONDS, "default_size": s.DEFAULT_PAGE_SIZE,
            "max_size": s.MAX_PAGE_SIZE, "max_query_len": s.MAX_QUERY_LEN,
            "max_query_terms": s.MAX_QUERY_TERMS, "typo_tolerance": s.TYPO_TOLERANCE,
            "suggest_max": s.SUGGEST_MAX, "list_limit": s.SEARCH_LIST_LIMIT}


def build_search(request: Request) -> SearchService:
    c = request.app.state.container
    return SearchService(index=c.search_index, cache=c.cache, config=_cfg(), event_bus=c.event_bus)


def _tenant(p: Principal) -> str:
    # HARD isolation: the search tenant ALWAYS comes from the caller's identity, never the body.
    return p.tenant or "__none__"


class SearchIn(BaseModel):
    index: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    text: str = ""
    filters: dict[str, Any] = Field(default_factory=dict)
    facets: list[str] = Field(default_factory=list)
    size: int | None = None
    cursor: str = ""
    highlight: bool = True


@router.post("", dependencies=[Depends(_READ)])
async def search(request: Request, body: SearchIn, principal: Principal = Depends(_READ)) -> dict:
    try:
        return await build_search(request).search(
            index=body.index, tenant=_tenant(principal), text=body.text, filters=body.filters,
            facets=body.facets, size=body.size, cursor=body.cursor, highlight=body.highlight)
    except QueryError as e:
        raise HTTPException(422, str(e))


@router.get("/suggest", dependencies=[Depends(_READ)])
async def suggest(request: Request, index: str, prefix: str, principal: Principal = Depends(_READ)) -> dict:
    return await build_search(request).suggest(index=index, tenant=_tenant(principal), prefix=prefix)


@router.get("/indexes/{index}/stats", dependencies=[Depends(_ADMIN)])
async def index_stats(request: Request, index: str) -> dict:
    return await build_search(request).stats(index=index)
