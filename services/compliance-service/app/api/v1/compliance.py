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
from app.core.consent_sweeper import ConsentSweeper
from app.core.dsar_service import DsarService
from app.core.models import DSAR_TYPES
from app.core.privacy_dispatcher import PrivacyDispatcher
from app.core.privacy_sweeper import PrivacySweeper
from app.core.retention import RetentionEnforcer
from app.core import pdpl_templates
from app.core.legal_gate import LegalGate, LegalGateError
from app.core.legal_pages import LegalError, LegalPageService
from app.core.pdpl_templates import TemplateError
from app.core.stores import (BreachStore, ConsentStore, DsarStore, HoldStore, LegalPageStore,
                             RopaStore, TargetStore)

router = APIRouter(prefix="/api/v1/compliance", tags=["compliance"])

_ADMIN = require_any_capability("platform.compliance", "compliance.manage")
_READ = require_any_capability("platform.compliance", "compliance.manage", "compliance.read")
# Reading a legal page is the lightest privilege here: any authenticated user must be able to see
# the terms they are being asked to accept. Authoring stays under _ADMIN.
_VIEW = require_any_capability("compliance.self", "compliance.read", "compliance.manage",
                              "platform.compliance")
# The data subject acting FOR THEMSELVES. Never resolves a subject from a body — it is always the
# caller's own token — so a self-service DSAR cannot be opened against anyone else.
_SELF = require_any_capability("compliance.self", "compliance.manage", "platform.compliance")


def _cfg() -> dict[str, Any]:
    s = get_settings()
    return {"sla_days": s.DSAR_SLA_DAYS, "verification_ttl": s.DSAR_VERIFICATION_TTL_SECONDS,
            "breach_hours": s.BREACH_NOTIFY_HOURS, "residency_default": s.DATA_RESIDENCY_DEFAULT,
            "list_limit": s.COMPLIANCE_LIST_LIMIT,
            # The names the dispatcher can actually reach. The data map and PRIVACY_TARGETS are
            # two halves of one fact and nothing used to compare them, so a target could be
            # registered for a service that was never deployed.
            "known_targets": tuple(s.privacy_targets),
            "hold_cache_ttl": s.HOLD_CACHE_TTL_SECONDS}


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
                             clock=c.clock, secrets=c.secrets, telemetry=c.telemetry,
                             credentials=getattr(request.app.state,
                                                 "operator_credentials", None))


def build_sweeper(request: Request) -> PrivacySweeper:
    s = get_settings()
    return PrivacySweeper(dsars=DsarStore(request.app.state.container.datastore),
                          dispatcher=build_dispatcher(request),
                          page_size=s.PRIVACY_SWEEP_PAGE_SIZE,
                          max_per_run=s.PRIVACY_SWEEP_MAX_PER_RUN,
                          telemetry=request.app.state.container.telemetry)


def build_consent_sweeper(request: Request) -> ConsentSweeper:
    """The watcher over guardian consents crossing majority. Runs on a CronJob, never a request."""
    c = request.app.state.container
    s = get_settings()
    return ConsentSweeper(store=c.datastore, consents=ConsentStore(c.datastore), clock=c.clock,
                          event_bus=c.event_bus,
                          notice_seconds=s.consent_lapse_notice_seconds,
                          max_per_run=s.CONSENT_SWEEP_MAX_PER_RUN, telemetry=c.telemetry,
                          # So the sweep can DERIVE a lapse date for rows granted before
                          # the policy existed, instead of skipping the one category it
                          # was built for.
                          capacity=CapacityPolicy(age_of_majority=s.CONSENT_AGE_OF_MAJORITY,
                                                  unknown_age=s.CONSENT_UNKNOWN_AGE))


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
                             clock=c.clock, config=_cfg(), event_bus=c.event_bus, cache=c.cache,
                             pii_salt=get_settings().PII_SALT)


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


class MeDsarIn(BaseModel):
    """A self-service DSAR. Carries only the KIND of request — never a subject id: the subject is
    the caller's own token, so this shape makes opening a request against someone else impossible,
    not merely forbidden (CWE-639)."""
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
        # AFTER the grant, not write-ahead. The write-ahead pattern exists so a CRITICAL action
        # cannot happen unwitnessed — it lets AuditUnavailable refuse the action. These routine
        # actions are fail-open by declaration, so the auditor never refuses them, and recording
        # first would only log grants that then failed validation.
        await request.app.state.auditor.record(
            "consent.granted", actor=principal.subject, tenant=_tenant(principal),
            resource=f"consent:{body.purpose}",
            ip=request.client.host if request.client else "")
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
    await request.app.state.auditor.record(
        "dsar.created", actor=principal.subject, tenant=_tenant(principal),
        resource=f"dsar:{d.id}", ip=request.client.host if request.client else "",
        metadata={"type": body.type, "created": created})
    out = d.public()
    out["created"] = created
    if created:
        out["verification_token"] = d.verification_token   # returned ONCE, to send to the subject
    return out


@router.post("/me/dsar", status_code=201)
async def create_my_dsar(request: Request, body: MeDsarIn,
                         principal: Principal = Depends(_SELF)) -> dict:
    """A data subject opens a DSAR about THEMSELVES.

    Subject and tenant are the caller's own token, never the body — the same DSAR machinery an
    operator drives, but with no way to name another subject. The verification token is returned
    once so the subject can confirm the request from the channel they own.
    """
    if body.type not in DSAR_TYPES:
        raise HTTPException(422, f"type must be one of {DSAR_TYPES}")
    tenant = _tenant(principal)
    d, created = await build_dsar(request).create(tenant=tenant, subject=principal.subject,
                                                  type=body.type, payload=body.payload)
    await request.app.state.auditor.record(
        "dsar.created", actor=principal.subject, tenant=tenant,
        resource=f"dsar:{d.id}", ip=request.client.host if request.client else "",
        metadata={"type": body.type, "created": created, "self_service": True})
    out = d.public()
    out["created"] = created
    if created:
        out["verification_token"] = d.verification_token
    return out


@router.post("/dsar/{dsar_id}/verify")
async def verify_dsar(request: Request, dsar_id: str, token: str,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    res = await build_dsar(request).verify(dsar_id, token)
    if not res["ok"]:
        raise HTTPException(400, res["reason"])
    # Verification is what turns a claim into an actionable request — the point the clock starts.
    await request.app.state.auditor.record(
        "dsar.verified", actor=principal.subject, tenant=_tenant(principal),
        resource=f"dsar:{dsar_id}", ip=request.client.host if request.client else "")
    return res


@router.post("/dsar/{dsar_id}/process")
async def process_dsar(request: Request, dsar_id: str,
                       principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "dsar.processed", actor=principal.subject, tenant=principal.tenant,
        resource=f"dsar:{dsar_id}",
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


@router.post("/legal-holds/{hold_id}/release")
async def release_hold(request: Request, hold_id: str,
                       principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "legal_hold.released", actor=principal.subject, tenant=principal.tenant,
        resource=f"hold:{hold_id}",
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
    try:
        t = await build_compliance(request).register_target(
            tenant=_tenant(principal), service=body.service, category=body.category,
            description=body.description)
    except ValueError as e:
        # 422, not 404: the request is well-formed and the caller is authorised — the NAME is the
        # problem. The message lists the configured targets, because the operator hitting this has
        # a typo or a service that was never deployed, and both are fixed by seeing the real set.
        raise HTTPException(422, str(e))
    # Its deletion was already audited; registering WIDENS the erasure fan-out and belongs in the
    # same record, so the two halves of a map change can be read together.
    await request.app.state.auditor.record(
        "data_target.registered", actor=principal.subject, tenant=principal.tenant,
        resource=f"target:{t.id}", ip=request.client.host if request.client else "",
        metadata={"service": body.service, "category": body.category})
    return t.to_dict()


@router.get("/data-targets/unreachable", dependencies=[Depends(_READ)])
async def unreachable_targets(request: Request, principal: Principal = Depends(_READ)) -> dict:
    """Targets on the map that PRIVACY_TARGETS no longer knows about.

    Registration refuses an unknown name, but PRIVACY_TARGETS is config: a target valid when it
    was registered goes stale the moment a deployment changes and the map does not. Finding that
    here costs a request; finding it during a DSAR costs the DSAR.
    """
    return {"unreachable": await build_compliance(request).unreachable_targets(_tenant(principal))}


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
    await request.app.state.auditor.record(
        "ropa.recorded", actor=principal.subject, tenant=_tenant(principal),
        resource=f"activity:{a.id}", ip=request.client.host if request.client else "",
        metadata={"name": a.name, "lawful_basis": a.lawful_basis})
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


@router.post("/breaches/{breach_id}/notify")
async def notify_breach(request: Request, breach_id: str,
                        principal: Principal = Depends(_ADMIN)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "breach.notified", actor=principal.subject, tenant=principal.tenant,
        resource=f"breach:{breach_id}",
        ip=request.client.host if request.client else "")
    res = await build_compliance(request).notify_breach(breach_id)
    if not res["ok"]:
        raise HTTPException(404, "NOT_FOUND")
    return res


@router.get("/breaches", dependencies=[Depends(_READ)])
async def list_breaches(request: Request, principal: Principal = Depends(_READ)) -> dict:
    return {"breaches": await build_compliance(request).list_breaches(_tenant(principal))}


# ── Legal / content pages ──────────────────────────────────────────────────────
def build_legal(request: Request) -> LegalPageService:
    c = request.app.state.container
    return LegalPageService(store=LegalPageStore(c.datastore), clock=c.clock, event_bus=c.event_bus)


class LegalDraftIn(BaseModel):
    # locale -> {"title": str, "sections": [{"heading","body"}, ...]}
    content: dict[str, Any] = Field(default_factory=dict)
    requires_acceptance: bool = False
    default_locale: str = Field(default="en", max_length=16)
    effective_at: float = 0.0


@router.put("/legal/{doc_id}/versions/{version}")
async def draft_legal(request: Request, doc_id: str, version: str, body: LegalDraftIn,
                      principal: Principal = Depends(_ADMIN)) -> dict:
    """Create or replace a DRAFT version. A published version is immutable — this refuses to edit one."""
    try:
        doc = await build_legal(request).draft(
            tenant=_tenant(principal), doc_id=doc_id, version=version, content=body.content,
            requires_acceptance=body.requires_acceptance, default_locale=body.default_locale,
            effective_at=body.effective_at)
    except LegalError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"doc_id": doc.doc_id, "version": doc.version, "status": doc.status}


@router.post("/legal/{doc_id}/versions/{version}/publish")
async def publish_legal(request: Request, doc_id: str, version: str,
                        principal: Principal = Depends(_ADMIN)) -> dict:
    """Publish a version and point 'current' at it — the event a fresh publish turns into re-acceptance."""
    await request.app.state.auditor.record(
        "legal.published", actor=principal.subject, tenant=_tenant(principal),
        resource=f"legal:{doc_id}:{version}",
        ip=request.client.host if request.client else "")
    try:
        doc = await build_legal(request).publish(tenant=_tenant(principal), doc_id=doc_id, version=version)
    except LegalError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"doc_id": doc.doc_id, "version": doc.version, "status": doc.status,
            "published_at": doc.published_at}


@router.get("/legal", dependencies=[Depends(_VIEW)])
async def list_legal(request: Request, principal: Principal = Depends(_VIEW)) -> dict:
    """Each document's current version + whether it needs acceptance — what a panel or gate lists."""
    return {"documents": await build_legal(request).list_documents(tenant=_tenant(principal))}


@router.get("/legal/{doc_id}", dependencies=[Depends(_VIEW)])
async def view_legal(request: Request, doc_id: str, locale: str = "", version: str = "",
                     principal: Principal = Depends(_VIEW)) -> dict:
    """The structured viewer payload: the current published version, or a specific one via ?version=."""
    svc = build_legal(request)
    tenant = _tenant(principal)
    view = (await svc.get_view(tenant=tenant, doc_id=doc_id, version=version, locale=locale)
            if version else await svc.current_view(tenant=tenant, doc_id=doc_id, locale=locale))
    if view is None:
        raise HTTPException(404, "NOT_FOUND")
    return view


@router.get("/legal/{doc_id}/history", dependencies=[Depends(_READ)])
async def legal_history(request: Request, doc_id: str, principal: Principal = Depends(_READ)) -> dict:
    return {"versions": await build_legal(request).history(tenant=_tenant(principal), doc_id=doc_id)}


# ── PDPL template pack: fill-in-the-blank Saudi-PDPL documents → a publishable page ─────
class InstantiateIn(BaseModel):
    values: dict[str, str] = Field(default_factory=dict)   # placeholder -> the tenant's value
    version: str = Field(..., min_length=1, max_length=64)
    requires_acceptance: bool = True
    publish: bool = False                                  # draft only, or publish immediately
    default_locale: str = Field(default="ar", max_length=16)


@router.get("/legal/templates", dependencies=[Depends(_READ)])
async def list_legal_templates(request: Request, principal: Principal = Depends(_READ)) -> dict:
    """The PDPL pack: each template's id, slug, bilingual title, and the placeholders it needs
    filled. A panel renders these as a form the operator completes."""
    return {"templates": pdpl_templates.list_templates()}


@router.post("/legal/templates/{template_id}/instantiate")
async def instantiate_legal_template(request: Request, template_id: str, body: InstantiateIn,
                                     principal: Principal = Depends(_ADMIN)) -> dict:
    """Fill a PDPL template with this tenant's values and DRAFT it as a legal page (optionally
    publish). Refuses if a declared placeholder is left unfilled — a policy with a literal {{HOLE}}
    must never reach a user. The page then lives in the normal legal-pages store: versioned,
    viewable, and (if requires_acceptance) gated by the post-auth access gate."""
    try:
        rendered = pdpl_templates.render(template_id, body.values, strict=True)
    except TemplateError as exc:
        raise HTTPException(422, str(exc)) from exc
    tenant = _tenant(principal)
    doc_id = rendered["slug"]
    legal = build_legal(request)
    # The rich blocks + the tenant's values, stored as-is. The renderer fills the tokens and picks
    # the language at read time — nothing is flattened, so the document keeps its tables and structure.
    content = {"title": rendered["title"], "blocks": rendered["blocks"], "values": rendered["values"]}
    try:
        await legal.draft(tenant=tenant, doc_id=doc_id, version=body.version,
                          content=content, requires_acceptance=body.requires_acceptance,
                          default_locale=body.default_locale)
    except LegalError as exc:
        raise HTTPException(409, str(exc)) from exc
    result = {"doc_id": doc_id, "version": body.version, "published": False}
    if body.publish:
        await request.app.state.auditor.record(
            "legal.published", actor=principal.subject, tenant=tenant,
            resource=f"legal:{doc_id}:{body.version}",
            ip=request.client.host if request.client else "")
        await legal.publish(tenant=tenant, doc_id=doc_id, version=body.version)
        result["published"] = True
    return result


# ── Post-auth access gate: the caller's OWN outstanding acceptances ─────────────
def build_legal_gate(request: Request) -> LegalGate:
    c = request.app.state.container
    return LegalGate(legal_store=LegalPageStore(c.datastore),
                     consents=build_consent(request), clock=c.clock)


@router.get("/me/legal/outstanding", dependencies=[Depends(_SELF)])
async def my_outstanding_legal(request: Request, locale: str = "",
                               principal: Principal = Depends(_SELF)) -> dict:
    """Which required legal documents the CALLER must (re-)accept before proceeding. The subject is
    the caller's own token — never a body — so this can only report on oneself. An app shell calls
    it right after login and blocks until the list is empty."""
    gate = build_legal_gate(request)
    return {"outstanding": await gate.outstanding(
        tenant=principal.tenant or "", subject=principal.subject, locale=locale)}


@router.post("/me/legal/{doc_id}/accept", dependencies=[Depends(_SELF)])
async def accept_legal(request: Request, doc_id: str,
                       principal: Principal = Depends(_SELF)) -> dict:
    """Record the CALLER's acceptance of a document's current published version. Pinned to the
    version in force now, so a later publish correctly leaves this acceptance stale."""
    try:
        res = await build_legal_gate(request).accept(
            tenant=principal.tenant or "", subject=principal.subject, doc_id=doc_id,
            actor=principal.subject, ip=request.client.host if request.client else "")
    except LegalGateError as exc:
        raise HTTPException(409, str(exc)) from exc
    return res
