# =============================================================================
# feature-flag-service — flags, versioned config, maintenance mode
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.config_service import ConfigService, MaintenanceService
from app.core.flag_service import FlagService
from app.core.models import Segment, iso
from app.core.stores import ConfigStore, FlagStore, MaintenanceStore, SegmentStore

router = APIRouter(prefix="/api/v1", tags=["flags"])

_ADMIN = require_any_capability("platform.flags", "flags.manage")
_READ = require_any_capability("platform.flags", "flags.manage", "flags.read")


def build_flags(request: Request) -> FlagService:
    c = request.app.state.container
    s = get_settings()
    return FlagService(store=FlagStore(c.datastore), provider=c.flag_provider,
                       entitlement=c.entitlement_checker, segments=SegmentStore(c.datastore),
                       cache=c.cache, event_bus=c.event_bus,
                       salt=s.ROLLOUT_SALT, list_limit=s.FLAG_LIST_LIMIT, stale_days=s.STALE_FLAG_DAYS,
                       cache_ttl=s.FLAG_CACHE_TTL_SECONDS,
                       last_seen_threshold=s.LAST_SEEN_WRITE_THRESHOLD_SECONDS,
                       exposure_sample_pct=s.EXPOSURE_SAMPLE_PCT,
                       prereq_max_depth=s.PREREQUISITE_MAX_DEPTH)


def build_config(request: Request) -> ConfigService:
    c = request.app.state.container
    s = get_settings()
    return ConfigService(store=ConfigStore(c.datastore), env=s.CONFIG_ENV,
                         event_bus=c.event_bus, list_limit=s.FLAG_LIST_LIMIT)


def build_maintenance(request: Request) -> MaintenanceService:
    c = request.app.state.container
    return MaintenanceService(store=MaintenanceStore(c.datastore), event_bus=c.event_bus,
                              bypass_subjects=get_settings().maintenance_bypass_subjects)


def build_segments(request: Request) -> SegmentStore:
    return SegmentStore(request.app.state.container.datastore)


class FlagIn(BaseModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    description: str | None = None
    enabled: bool | None = None
    default: bool | None = None
    rollout_pct: float | None = Field(default=None, ge=0, le=100)
    bucket_by: str | None = Field(default=None, pattern=r"^(user|tenant|attr:[A-Za-z0-9_]+)$")
    variants: dict[str, float] | None = None       # {name: weight} summing to 100
    rules: list[dict[str, Any]] | None = None
    segments: list[str] | None = None
    prerequisites: list[dict[str, Any]] | None = None
    requires_entitlement: str | None = None
    start: float | None = None
    end: float | None = None


class SegmentIn(BaseModel):
    key: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    description: str = ""
    rules: list[dict[str, Any]] = Field(default_factory=list)


class OverrideIn(BaseModel):
    scope: str = Field(pattern="^(tenant|user)$")
    subject: str = Field(min_length=1, max_length=128)
    value: bool


class EvalIn(BaseModel):
    tenant: str = ""
    user: str = ""
    attrs: dict[str, Any] = Field(default_factory=dict)
    keys: list[str] | None = None       # None → evaluate every flag


class ConfigIn(BaseModel):
    value: Any


class MaintenanceIn(BaseModel):
    module: str = Field(default="global", max_length=64, pattern=r"^[A-Za-z0-9._-]+$")
    enabled: bool = False
    message: str = ""
    start: float = 0.0
    end: float = 0.0


# ── flags (admin) ─────────────────────────────────────────────────────────────
@router.post("/flags", status_code=201)
async def upsert_flag(request: Request, body: FlagIn, principal: Principal = Depends(_ADMIN)) -> dict:
    try:
        flag = await build_flags(request).upsert(actor=principal.subject, **body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))            # e.g. variant weights don't sum to 100
    return flag.to_dict()


@router.get("/flags", dependencies=[Depends(_READ)])
async def list_flags(request: Request, include_archived: bool = False) -> dict:
    return {"flags": await build_flags(request).list(include_archived)}


@router.get("/flags/stale", dependencies=[Depends(_READ)])
async def stale_flags(request: Request, days: int | None = None) -> dict:
    """Flags not evaluated in N days — the cleanup worklist."""
    return {"stale": await build_flags(request).stale(days)}


@router.delete("/flags/{key}")
async def archive_flag(request: Request, key: str, principal: Principal = Depends(_ADMIN)) -> dict:
    flag = await build_flags(request).archive(key, actor=principal.subject)
    if flag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return flag.to_dict()


# ── segments (reusable audiences) ──────────────────────────────────────────────
@router.post("/segments", status_code=201, dependencies=[Depends(_ADMIN)])
async def upsert_segment(request: Request, body: SegmentIn) -> dict:
    seg = Segment(key=body.key, description=body.description, rules=body.rules, created=iso())
    return (await build_segments(request).save(seg)).to_dict()


@router.get("/segments", dependencies=[Depends(_READ)])
async def list_segments(request: Request) -> dict:
    return {"segments": await build_segments(request).list(get_settings().FLAG_LIST_LIMIT)}


@router.delete("/segments/{key}", dependencies=[Depends(_ADMIN)])
async def delete_segment(request: Request, key: str) -> dict:
    await build_segments(request).delete(key)
    return {"deleted": True, "key": key}


@router.post("/flags/{key}/overrides")
async def set_override(request: Request, key: str, body: OverrideIn,
                       principal: Principal = Depends(_ADMIN)) -> dict:
    subject = body.subject
    if body.scope == "tenant" and "platform.flags" not in principal.caps:
        subject = principal.tenant or "__none__"        # can only override your own tenant
    flag = await build_flags(request).set_override(key, scope=body.scope, subject=subject,
                                                   value=body.value, actor=principal.subject)
    if flag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return flag.to_dict()


@router.delete("/flags/{key}/overrides")
async def clear_override(request: Request, key: str, scope: str, subject: str,
                         principal: Principal = Depends(_ADMIN)) -> dict:
    if scope not in ("tenant", "user"):
        raise HTTPException(422, "BAD_SCOPE")
    if scope == "tenant" and "platform.flags" not in principal.caps:
        subject = principal.tenant or "__none__"
    flag = await build_flags(request).clear_override(key, scope=scope, subject=subject,
                                                     actor=principal.subject)
    if flag is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return flag.to_dict()


@router.post("/flags/evaluate", dependencies=[Depends(_READ)])
async def evaluate(request: Request, body: EvalIn) -> dict:
    svc = build_flags(request)
    ctx = {"tenant": body.tenant, "user": body.user, "attrs": body.attrs}
    if body.keys:
        return {"flags": {k: await svc.evaluate(k, ctx) for k in body.keys}}
    return {"flags": await svc.evaluate_all(ctx)}


# ── config (versioned) ─────────────────────────────────────────────────────────
@router.put("/config/{key}", dependencies=[Depends(_ADMIN)])
async def put_config(request: Request, key: str, body: ConfigIn,
                     principal: Principal = Depends(_ADMIN)) -> dict:
    return (await build_config(request).put(key, body.value, actor=principal.subject)).to_dict()


@router.get("/config/{key}", dependencies=[Depends(_READ)])
async def get_config(request: Request, key: str) -> dict:
    entry = await build_config(request).get(key)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return entry.to_dict()


@router.get("/config/{key}/history", dependencies=[Depends(_READ)])
async def config_history(request: Request, key: str) -> dict:
    return {"history": await build_config(request).history(key)}


@router.post("/config/{key}/rollback", dependencies=[Depends(_ADMIN)])
async def rollback_config(request: Request, key: str, version: int,
                          principal: Principal = Depends(_ADMIN)) -> dict:
    entry = await build_config(request).rollback(key, version, actor=principal.subject)
    if entry is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "VERSION_NOT_FOUND")
    return entry.to_dict()


# ── maintenance ───────────────────────────────────────────────────────────────
@router.put("/maintenance", dependencies=[Depends(_ADMIN)])
async def set_maintenance(request: Request, body: MaintenanceIn) -> dict:
    return (await build_maintenance(request).set(**body.model_dump())).to_dict()


@router.get("/maintenance", dependencies=[Depends(_READ)])
async def get_maintenance(request: Request, module: str = "global",
                          principal: Principal = Depends(_READ)) -> dict:
    return await build_maintenance(request).status(module, subject=principal.subject)
