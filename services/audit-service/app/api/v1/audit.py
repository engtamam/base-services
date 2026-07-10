# =============================================================================
# audit-service — admin query/verify/export/purge routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from starlette.responses import Response

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.adapters.exporter.csv_exporter import CsvExporter
from app.adapters.exporter.json_exporter import JsonExporter
from app.adapters.exporter.ndjson_exporter import NdjsonExporter
from app.config import get_settings
from app.core.audit_repository import AuditRepository
from app.core.audit_service import AuditService

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def build_audit(request: Request) -> AuditService:
    c = request.app.state.container
    s = get_settings()
    exporters = {"json": JsonExporter(), "csv": CsvExporter(), "ndjson": NdjsonExporter()}
    return AuditService(
        repo=AuditRepository(c.datastore), integrity=c.integrity, exporters=exporters,
        default_format=s.EXPORTER_DEFAULT, partition_mode=s.AUDIT_PARTITION,
        query_limit=s.AUDIT_QUERY_LIMIT,
    )


class PurgeIn(BaseModel):
    before_ts: str = Field(min_length=4, max_length=64)
    partition: str = "global"


def _scope_tenant(principal: Principal) -> str | None:
    """None = all tenants (platform.audit); else the caller's own tenant (audit.view)."""
    if "platform.audit" in principal.caps:
        return None
    if "audit.view" in principal.caps:
        return principal.tenant or "__none__"
    raise HTTPException(status.HTTP_403_FORBIDDEN, "FORBIDDEN")


def _filters(request: Request, principal: Principal) -> dict[str, Any]:
    q = request.query_params
    f: dict[str, Any] = {}
    for key in ("service", "actor", "action", "resource", "since", "until"):
        if q.get(key):
            f[key] = q[key]
    scope = _scope_tenant(principal)
    if scope is not None:
        f["tenant"] = scope           # tenant-admin: forced to own tenant
    elif q.get("tenant"):
        f["tenant"] = q["tenant"]     # platform: optional tenant filter
    return f


@router.get("")
async def query(request: Request, principal: Principal = Depends(require_user),
                guard=Depends(get_guard), limit: int | None = Query(default=None, le=1000)) -> dict:
    events = await build_audit(request).query(_filters(request, principal), limit)
    async with guard.step("respond") as step:
        body = {"events": events, "count": len(events)}
        step.result = body
    return body


@router.get("/verify", dependencies=[Depends(require_capability("platform.audit"))])
async def verify(request: Request, partition: str = Query(default="global")) -> dict:
    return await build_audit(request).verify(partition)


@router.get("/export")
async def export(request: Request, principal: Principal = Depends(require_user),
                 fmt: str = Query(default="", alias="format")) -> Response:
    content_type, body = await build_audit(request).export(_filters(request, principal), fmt or None)
    return Response(content=body, media_type=content_type,
                    headers={"Content-Disposition": "attachment; filename=audit-export"})


@router.post("/purge", dependencies=[Depends(require_capability("platform.audit"))])
async def purge(request: Request, body: PurgeIn) -> dict:
    return await build_audit(request).purge(body.before_ts, body.partition)
