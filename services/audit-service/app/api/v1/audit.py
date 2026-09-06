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
from app.core.pii_policy import PiiPolicy, parse_fields

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])


def build_audit(request: Request) -> AuditService:
    c = request.app.state.container
    s = get_settings()
    exporters = {"json": JsonExporter(), "csv": CsvExporter(), "ndjson": NdjsonExporter()}
    return AuditService(
        repo=AuditRepository(c.datastore), integrity=c.integrity, exporters=exporters,
        default_format=s.EXPORTER_DEFAULT, partition_mode=s.AUDIT_PARTITION,
        query_limit=s.AUDIT_QUERY_LIMIT,
        export_limit=s.AUDIT_EXPORT_LIMIT,
        # Applied before the event is hashed. Without this the pseudonym rule is a hope: one
        # forgotten call site anywhere in the platform puts a person in the chain forever.
        pii_policy=PiiPolicy(mode=s.AUDIT_PII_MODE,
                             fields=parse_fields(s.AUDIT_PII_FIELDS),
                             salt=s.PII_SALT),
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
    """Build the query filter, honouring the tenancy wall.

    The values are read from the raw query string, but every one of them is ALSO declared on the
    routes below so that it appears in the generated OpenAPI spec and gets a length bound. They
    worked before and were invisible: a generated client, the admin catalog and the API reference
    were all blind to seven filters that had always been there.
    """
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
                guard=Depends(get_guard), limit: int | None = Query(default=None, le=1000),
                # DECLARED, not read from request.query_params. These filters always worked, and
                # because they were never declared they appeared in no OpenAPI spec — so a client
                # generated from the spec could not discover them, a console could not offer them,
                # and nothing validated their length. An audit log that cannot be narrowed by actor
                # or action is unusable at the size an audit log reaches.
                service: str | None = Query(default=None, max_length=128),
                actor: str | None = Query(default=None, max_length=256),
                action: str | None = Query(default=None, max_length=128),
                resource: str | None = Query(default=None, max_length=256),
                since: str | None = Query(default=None, max_length=64),
                until: str | None = Query(default=None, max_length=64),
                tenant: str | None = Query(default=None, max_length=128),
                after: str | None = Query(default=None, max_length=512)) -> dict:
    # Recorded AFTER the read, not write-ahead. A read is not a mutation, so there is nothing to
    # protect by recording first — and this action is non-critical, so the auditor never refuses
    # it. Recording first would only log lookups that then failed.
    #
    # Yes, this means querying the audit log appends to the audit log. That is intended: an
    # access trail is only worth having if reading it is also on it. It cannot runaway — one entry
    # per request, and an entry is not itself a query.
    events, next_cursor = await build_audit(request).query(
        _filters(request, principal), limit, after=after)
    await request.app.state.auditor.record(
        "audit.queried", actor=principal.subject, tenant=principal.tenant, resource="audit_log",
        ip=request.client.host if request.client else "",
        metadata={"returned": len(events)})
    async with guard.step("respond") as step:
        # next_cursor: pass back as `after` to page a tenant-scoped query past `limit`; null when the
        # walk reached the end, or for the cross-partition (platform, no-tenant) path which is not
        # pageable — narrow to a tenant to page. See AuditService.query.
        body = {"events": events, "count": len(events), "next_cursor": next_cursor}
        step.result = body
    return body


@router.get("/verify", dependencies=[Depends(require_capability("platform.audit"))])
async def verify(request: Request, partition: str = Query(default="global")) -> dict:
    return await build_audit(request).verify(partition)


@router.get("/export")
async def export(request: Request, principal: Principal = Depends(require_user),
                 fmt: str = Query(default="", alias="format"),
                 # Same filters as the query above, and declared for the same reason: an export
                 # that cannot be narrowed is a full extraction of the log every time somebody
                 # wants one investigation's worth of it.
                 service: str | None = Query(default=None, max_length=128),
                 actor: str | None = Query(default=None, max_length=256),
                 action: str | None = Query(default=None, max_length=128),
                 resource: str | None = Query(default=None, max_length=256),
                 since: str | None = Query(default=None, max_length=64),
                 until: str | None = Query(default=None, max_length=64),
                 tenant: str | None = Query(default=None, max_length=128)) -> Response:
    # Write-ahead, unlike the query above, because this one IS critical: a bulk extraction of the
    # whole log that we could not record is refused rather than served. Recording afterwards would
    # mean the export had already left the building by the time we failed to witness it.
    await request.app.state.auditor.record(
        "audit.exported", actor=principal.subject, tenant=principal.tenant, resource="audit_log",
        ip=request.client.host if request.client else "",
        metadata={"format": fmt or "default"})
    content_type, body = await build_audit(request).export(_filters(request, principal), fmt or None)
    return Response(content=body, media_type=content_type,
                    headers={"Content-Disposition": "attachment; filename=audit-export"})


@router.post("/purge")
async def purge(request: Request, body: PurgeIn,
                principal: Principal = Depends(require_capability("platform.audit"))) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "audit.purged", actor=principal.subject, tenant=principal.tenant, resource="audit_log",
        ip=request.client.host if request.client else "")
    return await build_audit(request).purge(body.before_ts, body.partition)
