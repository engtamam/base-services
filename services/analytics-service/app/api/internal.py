# =============================================================================
# analytics-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.config import Settings, get_settings
from app.core.erasure import AnalyticsErasure
from app.core.ingest_service import IngestService
from app.core.stores import CardinalityStore, EventStore, RollupStore
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


def _icfg(s: Settings) -> dict[str, Any]:
    return {"pii_allowed": s.pii_allowed_properties, "max_cardinality": s.MAX_DIMENSION_CARDINALITY,
            "late_max_seconds": s.LATE_EVENT_MAX_SECONDS, "hll_precision": s.HLL_PRECISION,
            "hist_buckets": s.HISTOGRAM_BUCKETS, "raw_retention_days": s.RAW_RETENTION_DAYS}


def build_ingest(request: Request) -> IngestService:
    c = request.app.state.container
    return IngestService(events=EventStore(c.datastore), rollups=RollupStore(c.datastore),
                         cardinality=CardinalityStore(c.datastore), clock=c.clock,
                         config=_icfg(get_settings()), cache=c.cache, event_bus=c.event_bus)


class EventIn(BaseModel):
    id: str = Field(min_length=1, max_length=200)   # the idempotency key
    name: str = Field(min_length=1, max_length=200)
    tenant: str = Field(min_length=1, max_length=200)
    event_time: float | None = None                 # when it HAPPENED; defaults to now
    subject: str = ""                               # user/session id — the unique-count identity
    value: float = 0.0
    props: dict[str, Any] | None = None


class BatchIn(BaseModel):
    tenant: str = Field(min_length=1, max_length=200)
    events: list[dict[str, Any]] = Field(min_length=1, max_length=1000)

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


@router.post("/analytics/ingest", dependencies=[Depends(require_internal_token)])
async def internal_ingest(body: EventIn, request: Request) -> dict:
    """Primary write path. Idempotent by `body.id` — our EventBus is at-least-once, so a
    redelivery must perform zero additional writes. Double-counted revenue is a wrong MRR."""
    return await build_ingest(request).ingest(
        event_id=body.id, tenant=body.tenant, name=body.name, event_time=body.event_time,
        subject=body.subject, value=body.value, props=body.props)


@router.post("/analytics/ingest/batch", dependencies=[Depends(require_internal_token)])
async def internal_ingest_batch(body: BatchIn, request: Request) -> dict:
    try:
        return await build_ingest(request).ingest_batch(body.tenant, body.events)
    except KeyError as e:
        raise HTTPException(status_code=400, detail="EVENT_MISSING_ID_OR_NAME") from e


@router.post("/analytics/prune-raw", dependencies=[Depends(require_internal_token)])
async def internal_prune_raw(request: Request) -> dict:
    """Apply RAW_RETENTION_DAYS: raw events age out, rollups persist. A scheduler calls this."""
    return await build_ingest(request).prune_raw()


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


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
    eraser = AnalyticsErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
