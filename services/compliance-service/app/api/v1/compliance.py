# =============================================================================
# compliance-service — consent, DSAR, legal hold, RoPA, breach, data map
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Principal
from app.api.authz import require_any_capability
from app.config import get_settings
from app.core.capacity import CapacityPolicy, CapacityRefused
from app.core.compliance_service import ComplianceService
from app.core.consent_service import ConsentService
from app.core.dsar_service import DsarService
from app.core.models import DSAR_TYPES
from app.core.privacy_dispatcher import PrivacyDispatcher
from app.core.privacy_sweeper import PrivacySweeper
from app.core.retention import RetentionEnforcer
from app.core.stores import (BreachStore, ConsentStore, DsarStore, HoldStore, RopaStore,
                             TargetStore)

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

_ADMIN = require_any_capability("platform.compliance", "compliance.manage")
_READ = require_any_capability("platform.compliance", "compliance.manage", "compliance.read")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"sla_days": s.DSAR_SLA_DAYS, "verification_ttl": s.DSAR_VERIFICATION_TTL_SECONDS,
            "breach_hours": s.BREACH_NOTIFY_HOURS, "residency_default": s.DATA_RESIDENCY_DEFAULT,
            "list_limit": s.COMPLIANCE_LIST_LIMIT}


def build_consent(request: Request) -> ConsentService:
    c = request.app.state.container
    s = get_settings()
    return ConsentService(store=ConsentStore(c.datastore), clock=c.clock, cache=c.cache,
                          cache_ttl=s.CONSENT_CACHE_TTL_SECONDS,
                          list_limit=s.COMPLIANCE_LIST_LIMIT, event_bus=c.event_bus,
                          capacity=CapacityPolicy(age_of_majority=s.CONSENT_AGE_OF_MAJORITY,
                                                  unknown_age=s.CONSENT_UNKNOWN_AGE))


def build_dsar(request: Request) -> DsarService:
    c = request.app.state.container
    return DsarService(dsars=DsarStore(c.datastore), holds=HoldStore(c.datastore),
                       targets=TargetStore(c.datastore), orchestrator=c.orchestrator,
                       clock=c.clock, config=_cfg(), event_bus=c.event_bus,
                       dispatcher=build_dispatcher(request))


def build_dispatcher(request: Request) -> PrivacyDispatcher:
    """The erasure fan-out. Targets and credentials come from CONFIG only — never from a request —
    so no caller can steer an erase POST at a host of their choosing."""
    c = request.app.state.container
    return PrivacyDispatcher(dsars=DsarStore(c.datastore), targets=TargetStore(c.datastore),
                             transport=c.privacy_transport, settings=get_settings(),
                             clock=c.clock, secrets=c.secrets, telemetry=c.telemetry)


def build_sweeper(request: Request) -> PrivacySweeper:
    s = get_settings()
    return PrivacySweeper(dsars=DsarStore(request.app.state.container.datastore),
                          dispatcher=build_dispatcher(request),
                          page_size=s.PRIVACY_SWEEP_PAGE_SIZE,
                          max_per_run=s.PRIVACY_SWEEP_MAX_PER_RUN,
                          telemetry=request.app.state.container.telemetry)


def build_retention(request: Request) -> RetentionEnforcer:
    c = request.app.state.container
    s = get_settings()
    return RetentionEnforcer(store=c.datastore, holds=HoldStore(c.datastore),
                             rules=s.retention_rules, clock=c.clock,
                             max_per_run=s.RETENTION_MAX_PER_RUN,
                             telemetry=c.telemetry)


def build_compliance(request: Request) -> ComplianceService:
    c = request.app.state.container
    return ComplianceService(holds=HoldStore(c.datastore), ropa=RopaStore(c.datastore),
                             breaches=BreachStore(c.datastore), targets=TargetStore(c.datastore),
                             clock=c.clock, config=_cfg(), event_bus=c.event_bus)


def _tenant(p: Principal, body_tenant: str | None = None) -> str:
    if "platform.compliance" in p.caps:
        return body_tenant or p.tenant or "global"
    return p.tenant or "__none__"


class ConsentIn(BaseModel):
    subject: str = Field(min_length=1, max_length=256)
    purpose: str = Field(min_length=1, max_length=128)
    notice_version: str = ""
    method: str = ""
    expires_at: float = 0.0
    # ── capacity (Phase 39) ─────────────────────────────────────────────────
    # Who is agreeing, when that is not the subject themself. Optional, so every existing caller
    # keeps working: absent means self-consent, which is what they were already asserting.
    basis: str = "self"                 # self | guardian | legal_representative
    consented_by: str = Field(default="", max_length=256)
    relation: str = Field(default="", max_length=128)
    subject_born_at: float = 0.0        # epoch seconds; 0 = unknown, NOT "adult"


class DsarIn(BaseModel):
    subject: str = Field(min_length=1, max_length=256)
    type: str
    payload: dict[str, Any] = Field(default_factory=dict)


class HoldIn(BaseModel):
    subject: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=512)


class TargetIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    description: str = ""


class RopaIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    purpose: str = ""
    lawful_basis: str = "consent"
    data_categories: list[str] = Field(default_factory=list)
    retention_days: int = 0
    residency: str | None = None
    cross_border: bool = False
    recipients: list[str] = Field(default_factory=list)


class BreachIn(BaseModel):
    description: str = Field(min_length=1, max_length=2048)
    discovered_at: float = 0.0
    affected_subjects: int = 0
    data_categories: list[str] = Field(default_factory=list)
    severity: str = "medium"


# ── consent ───────────────────────────────────────────────────────────────────
@router.post("/consent/grant", status_code=201)
async def grant(request: Request, body: ConsentIn, principal: Principal = Depends(_ADMIN)) -> dict:
    try:
        c = await build_consent(request).grant(
            tenant=_tenant(principal), subject=body.subject, purpose=body.purpose,
            notice_version=body.notice_version, method=body.method, expires_at=body.expires_at,
            actor=principal.subject, ip=request.client.host if request.client else "",
            basis=body.basis, consented_by=body.consented_by, relation=body.relation,
            subject_born_at=body.subject_born_at)
        if body.basis != "self":
            # Delegated consent only. A self-consent is the ordinary case and recording every one
            # would bury the log; the exception is the claim someone may later dispute.
            await request.app.state.auditor.record(
                "consent.guardian_recorded", actor=principal.subject,
                tenant=_tenant(principal), resource=f"consent:{body.purpose}",
                ip=request.client.host if request.client else "",
                metadata={"basis": body.basis, "relation": body.relation})
    except CapacityRefused as exc:
        # 422, not 400: the request is well-formed and the caller is authorised — the consent is
        # simply one we may not lawfully record. The reason is returned because the caller has to
        # fix it (supply a birth date, or name a guardian), and it names no personal data.
        raise HTTPException(422, str(exc)) from exc
    return c.to_dict()


@router.post("/consent/withdraw")
async def withdraw(request: Request, body: ConsentIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "consent.withdrawn", actor=principal.subject, tenant=principal.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = await build_consent(request).withdraw(
        tenant=_tenant(principal), subject=body.subject, purpose=body.purpose,
        actor=principal.subject, ip=request.client.host if request.client else "")
    return c.to_dict()


@router.get("/consent/history", dependencies=[Depends(_READ)])
async def consent_history(request: Request, subject: str, principal: Principal = Depends(_READ)) -> dict:
    return await build_consent(request).history(_tenant(principal), subject)


# ── DSAR ───────────────────────────────────────────────────────────────────────
@router.post("/dsar", status_code=201)
async def create_dsar(request: Request, body: DsarIn, principal: Principal = Depends(_ADMIN)) -> dict:
    if body.type not in DSAR_TYPES:
        raise HTTPException(422, f"type must be one of {DSAR_TYPES}")
    d, created = await build_dsar(request).create(tenant=_tenant(principal), subject=body.subject,
                                                  type=body.type, payload=body.payload)
    out = d.public()
    out["created"] = created
    if created:
        out["verification_token"] = d.verification_token   # returned ONCE, to send to the subject
    return out


@router.post("/dsar/{dsar_id}/verify")
async def verify_dsar(request: Request, dsar_id: str, token: str,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    res = await build_dsar(request).verify(dsar_id, token)
    if not res["ok"]:
        raise HTTPException(400, res["reason"])
    return res


@router.post("/dsar/{dsar_id}/process", dependencies=[Depends(_ADMIN)])
async def process_dsar(request: Request, dsar_id: str) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "dsar.processed", actor="", tenant="", resource=f"dsar:{dsar_id}",
        ip=request.client.host if request.client else "")
    res = await build_dsar(request).process(dsar_id)
    if not res.get("ok") and res.get("reason") == "NOT_FOUND":
        raise HTTPException(404, "NOT_FOUND")
    return res


@router.get("/dsar", dependencies=[Depends(_READ)])
async def list_dsar(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"requests": await build_dsar(request).list(_tenant(principal))}


# ── legal hold ─────────────────────────────────────────────────────────────────
@router.post("/legal-holds", status_code=201)
async def place_hold(request: Request, body: HoldIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "legal_hold.placed", actor=principal.subject, tenant=principal.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    return (await build_compliance(request).place_hold(
        tenant=_tenant(principal), subject=body.subject, reason=body.reason)).to_dict()


@router.post("/legal-holds/{hold_id}/release", dependencies=[Depends(_ADMIN)])
async def release_hold(request: Request, hold_id: str) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "legal_hold.released", actor="", tenant="", resource=f"hold:{hold_id}",
        ip=request.client.host if request.client else "")
    h = await build_compliance(request).release_hold(hold_id)
    if h is None:
        raise HTTPException(404, "NOT_FOUND")
    return h.to_dict()


@router.get("/legal-holds", dependencies=[Depends(_READ)])
async def list_holds(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"holds": await build_compliance(request).list_holds(_tenant(principal))}


# ── data map ───────────────────────────────────────────────────────────────────
@router.post("/data-targets", status_code=201)
async def register_target(request: Request, body: TargetIn, principal: Principal = Depends(_ADMIN)) -> dict:
    return (await build_compliance(request).register_target(
        tenant=_tenant(principal), service=body.service, category=body.category,
        description=body.description)).to_dict()


@router.get("/data-targets", dependencies=[Depends(_READ)])
async def list_targets(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"targets": await build_compliance(request).list_targets(_tenant(principal))}


@router.delete("/data-targets/{target_id}")
async def delete_target(request: Request, target_id: str,
                        principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    #
    # Audited even though registering one is not: removing a target NARROWS the erasure
    # fan-out, so a DSAR after this reaches fewer stores and still reports complete. If that
    # is ever wrong, the log is the only thing that shows when the map changed and who did it.
    await request.app.state.auditor.record(
        "data_target.deleted", actor=principal.subject, tenant=principal.tenant,
        resource=f"target:{target_id}", ip=request.client.host if request.client else "")
    if not await build_compliance(request).delete_target(_tenant(principal), target_id):
        raise HTTPException(404, "NOT_FOUND")
    return {"deleted": True}


# ── RoPA ───────────────────────────────────────────────────────────────────────
@router.post("/ropa", status_code=201)
async def create_activity(request: Request, body: RopaIn, principal: Principal = Depends(_ADMIN)) -> dict:
    try:
        a = await build_compliance(request).create_activity(tenant=_tenant(principal), **body.model_dump())
    except ValueError as e:
        raise HTTPException(422, str(e))
    return a.to_dict()


@router.get("/ropa", dependencies=[Depends(_READ)])
async def list_activities(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"activities": await build_compliance(request).list_activities(_tenant(principal))}


@router.delete("/ropa/{activity_id}")
async def delete_activity(request: Request, activity_id: str,
                          principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead, and for the strongest reason in this file: Art. 30 obliges the controller to
    # MAINTAIN the record of processing. An entry that vanishes with nothing behind it is the
    # one outcome that cannot be defended to a regulator, so the log entry precedes the delete.
    await request.app.state.auditor.record(
        "ropa_activity.deleted", actor=principal.subject, tenant=principal.tenant,
        resource=f"activity:{activity_id}", ip=request.client.host if request.client else "")
    if not await build_compliance(request).delete_activity(_tenant(principal), activity_id):
        raise HTTPException(404, "NOT_FOUND")
    return {"deleted": True}


# ── breach register (72-hour clock) ────────────────────────────────────────────
@router.post("/breaches", status_code=201)
async def register_breach(request: Request, body: BreachIn, principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "breach.recorded", actor=principal.subject, tenant=principal.tenant, resource="breach",
        ip=request.client.host if request.client else "")
    b = await build_compliance(request).register_breach(tenant=_tenant(principal), **body.model_dump())
    return await build_compliance(request).breach_status(b.id)


@router.post("/breaches/{breach_id}/notify", dependencies=[Depends(_ADMIN)])
async def notify_breach(request: Request, breach_id: str) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "breach.notified", actor="", tenant="", resource=f"breach:{breach_id}",
        ip=request.client.host if request.client else "")
    res = await build_compliance(request).notify_breach(breach_id)
    if not res["ok"]:
        raise HTTPException(404, "NOT_FOUND")
    return res


@router.get("/breaches", dependencies=[Depends(_READ)])
async def list_breaches(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"breaches": await build_compliance(request).list_breaches(_tenant(principal))}
