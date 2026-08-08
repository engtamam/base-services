# =============================================================================
# analytics-service — query / funnel / retention / saved reports / export
# -----------------------------------------------------------------------------
# Read tier. Deny-by-default: every route demands a capability. Tenant isolation
# is enforced HERE, not by the caller: a non-platform principal can only ever
# read its own tenant's rollups (CWE-639 IDOR).
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.models import ReportDef, new_id
from app.core.query import QueryError
from app.core.query_service import QueryService
from app.core.stores import EventStore, ReportStore, RollupStore

router = APIRouter(prefix="/api/v1/analytics", tags=["analytics"])

_PLATFORM = "platform.analytics"
_MANAGE = require_any_capability(_PLATFORM, "analytics.manage")
_READ = require_any_capability(_PLATFORM, "analytics.manage", "analytics.read")


def _qcfg() -> dict[str, Any]:
    s = get_settings()
    return {"cache_ttl": s.QUERY_CACHE_TTL_SECONDS, "max_range_days": s.MAX_QUERY_RANGE_DAYS,
            "max_groups": s.MAX_GROUPS, "tz_offset": s.DEFAULT_TIMEZONE_OFFSET_MINUTES}


def build_query(request: Request) -> QueryService:
    c = request.app.state.container
    return QueryService(rollups=RollupStore(c.datastore), events=EventStore(c.datastore),
                        warehouse=c.warehouse, clock=c.clock, config=_qcfg(), cache=c.cache)


def _tenant(p: Principal, requested: str | None = None) -> str:
    """Platform callers may target any tenant; everyone else is pinned to their own."""
    if _PLATFORM in p.caps:
        return requested or p.tenant or "global"
    if requested and requested != p.tenant:
        raise HTTPException(status_code=403, detail="FORBIDDEN")
    return p.tenant or "__none__"


class QueryIn(BaseModel):
    metric: str
    agg: str = "count"
    granularity: str = "day"
    start_ts: float
    end_ts: float
    dimensions: list[str] | None = None
    filters: dict[str, Any] | None = None
    percentile: float | None = None
    tz_offset_minutes: int | None = None
    tenant: str | None = None


class FunnelIn(BaseModel):
    steps: list[str] = Field(min_length=2, max_length=10)
    start_ts: float
    end_ts: float
    window_seconds: int = Field(default=86_400, gt=0)
    tenant: str | None = None


class RetentionIn(BaseModel):
    event_name: str
    cohort_start: float
    cohort_end: float
    periods: int = Field(default=7, gt=0, le=52)
    period_seconds: int = Field(default=86_400, gt=0)
    tenant: str | None = None


class ReportIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    query: QueryIn


@router.post("/query", dependencies=[Depends(rate_limit)])
async def run_query(body: QueryIn, request: Request, p: Principal = Depends(_READ)) -> dict:
    """Reads ROLLUPS ONLY — never the raw event table. Cached by tenant generation."""
    tenant = _tenant(p, body.tenant)
    try:
        result = await build_query(request).query(
            tenant=tenant, metric=body.metric, agg=body.agg,
            granularity=body.granularity, start_ts=body.start_ts, end_ts=body.end_ts,
            dimensions=body.dimensions, filters=body.filters, percentile=body.percentile,
            tz_offset_minutes=body.tz_offset_minutes)
    except QueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await request.app.state.auditor.record(
        "analytics.queried", actor=p.subject, tenant=tenant, resource="query",
        ip=request.client.host if request.client else "", metadata={"metric": body.metric})
    return result


@router.post("/funnel", dependencies=[Depends(rate_limit)])
async def run_funnel(body: FunnelIn, request: Request, p: Principal = Depends(_READ)) -> dict:
    """Sequence question — reads raw events, bounded by the range. Not the hot path."""
    return await build_query(request).funnel(
        tenant=_tenant(p, body.tenant), steps=body.steps, start_ts=body.start_ts,
        end_ts=body.end_ts, window_seconds=body.window_seconds)


@router.post("/retention", dependencies=[Depends(rate_limit)])
async def run_retention(body: RetentionIn, request: Request, p: Principal = Depends(_READ)) -> dict:
    return await build_query(request).retention(
        tenant=_tenant(p, body.tenant), event_name=body.event_name,
        cohort_start=body.cohort_start, cohort_end=body.cohort_end,
        periods=body.periods, period_seconds=body.period_seconds)


# ── saved reports ───────────────────────────────────────────────────────────
@router.post("/reports", status_code=201, dependencies=[Depends(rate_limit)])
async def create_report(body: ReportIn, request: Request, p: Principal = Depends(_MANAGE)) -> dict:
    c = request.app.state.container
    tenant = _tenant(p, body.query.tenant)
    r = ReportDef(id=new_id("rep_"), tenant=tenant, name=body.name,
                  query=body.query.model_dump(exclude_none=True), created=c.clock.now())
    await ReportStore(c.datastore).save(r)
    await request.app.state.auditor.record(
        "analytics.report_created", actor=p.subject, tenant=tenant,
        resource=f"report:{r.id}", ip=request.client.host if request.client else "")
    return r.to_dict()


@router.get("/reports", dependencies=[Depends(rate_limit)])
async def list_reports(request: Request, p: Principal = Depends(_READ)) -> dict:
    c = request.app.state.container
    rows = await ReportStore(c.datastore).list(_tenant(p), get_settings().ANALYTICS_LIST_LIMIT)
    return {"reports": rows, "count": len(rows)}


async def _owned_report(request: Request, report_id: str, p: Principal) -> ReportDef:
    r = await ReportStore(request.app.state.container.datastore).get(report_id)
    if r is None or (_PLATFORM not in p.caps and r.tenant != p.tenant):
        raise HTTPException(status_code=404, detail="NOT_FOUND")   # never confirm existence
    return r


@router.get("/reports/{report_id}", dependencies=[Depends(rate_limit)])
async def get_report(report_id: str, request: Request, p: Principal = Depends(_READ)) -> dict:
    return (await _owned_report(request, report_id, p)).to_dict()


@router.post("/reports/{report_id}/run", dependencies=[Depends(rate_limit)])
async def run_report(report_id: str, request: Request, p: Principal = Depends(_READ)) -> dict:
    r = await _owned_report(request, report_id, p)
    q = dict(r.query)
    q.pop("tenant", None)
    try:
        return await build_query(request).query(tenant=r.tenant, **q)
    except (QueryError, TypeError) as e:
        raise HTTPException(status_code=400, detail="BAD_REPORT_QUERY") from e


@router.put("/reports/{report_id}", dependencies=[Depends(rate_limit)])
async def update_report(report_id: str, body: ReportIn, request: Request,
                        p: Principal = Depends(_MANAGE)) -> dict:
    """Edit a saved report's name or query in place. Without this, changing a report meant deleting
    and recreating it — losing its id, and any dashboard or link that referenced it. Ownership is
    checked; id, tenant and created are preserved."""
    existing = await _owned_report(request, report_id, p)
    c = request.app.state.container
    r = ReportDef(id=existing.id, tenant=existing.tenant, name=body.name,
                  query=body.query.model_dump(exclude_none=True), created=existing.created)
    await ReportStore(c.datastore).save(r)
    return r.to_dict()


@router.delete("/reports/{report_id}", status_code=204, dependencies=[Depends(rate_limit)])
async def delete_report(report_id: str, request: Request, p: Principal = Depends(_MANAGE)) -> Response:
    r = await _owned_report(request, report_id, p)
    await ReportStore(request.app.state.container.datastore).delete(r.id)
    await request.app.state.auditor.record(
        "analytics.report_deleted", actor=p.subject, tenant=r.tenant,
        resource=f"report:{r.id}", ip=request.client.host if request.client else "")
    return Response(status_code=204)


# ── export ──────────────────────────────────────────────────────────────────
@router.post("/export", dependencies=[Depends(rate_limit)])
async def export_query(body: QueryIn, request: Request, p: Principal = Depends(_READ)) -> Response:
    """Run a query and hand it to the configured Warehouse port (CSV by default)."""
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "analytics.exported", actor=p.subject, tenant=p.tenant, resource="analytics_export",
        ip=request.client.host if request.client else "")
    svc = build_query(request)
    try:
        result = await svc.query(
            tenant=_tenant(p, body.tenant), metric=body.metric, agg=body.agg,
            granularity=body.granularity, start_ts=body.start_ts, end_ts=body.end_ts,
            dimensions=body.dimensions, filters=body.filters, percentile=body.percentile,
            tz_offset_minutes=body.tz_offset_minutes)
    except QueryError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    rows = result.get("series") or [{"group": g, "value": v}
                                    for g, v in (result.get("groups") or {}).items()]
    out = svc.export(rows)
    if out is None:
        # The null warehouse does not pretend to export. Say so; don't return an empty file.
        raise HTTPException(status_code=501, detail="WAREHOUSE_NOT_CONFIGURED")
    content_type, payload = out
    return Response(content=payload, media_type=content_type,
                    headers={"Content-Disposition": 'attachment; filename="analytics.csv"'})
