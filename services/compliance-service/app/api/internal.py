# =============================================================================
# compliance-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.compliance import (build_compliance, build_consent, build_consent_sweeper,
                                   build_dsar, build_retention, build_sweeper)
from app.config import Settings, get_settings
from app.core.erasure import ComplianceErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class AckIn(BaseModel):
    service: str = Field(min_length=1, max_length=128)
    category: str = Field(min_length=1, max_length=128)
    # The ack BODY from an async (202) target: what it erased, what it kept and why. Optional so
    # the existing contract still works, but without it the DSAR records THAT a target finished
    # and not WHAT it did — and "what was kept, on what basis" is the half a regulator asks about.
    ack: dict | None = None

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


@router.get("/health", dependencies=[Depends(require_internal_token)])
async def internal_health(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    return {"status": "ok", "service": settings.SERVICE_NAME}


@router.get("/compliance/consent-check", dependencies=[Depends(require_internal_token)])
async def consent_check(request: Request, tenant: str, subject: str, purpose: str) -> dict:
    """HOT PATH — any service asks before processing personal data. Default DENY, cached."""
    return await build_consent(request).check(tenant=tenant, subject=subject, purpose=purpose)


@router.get("/compliance/hold-check", dependencies=[Depends(require_internal_token)])
async def hold_check(request: Request, tenant: str, subject: str) -> dict:
    """HOT PATH — a service asks before PURGING. Fail closed, cached by generation.

    Retention is decentralised: every service enforces its own period, and retention.py is
    explicit that a hold beats retention and a held row must be reported as RETAINED rather than
    silently skipped. Until now that was impossible to honour from outside — holds lived only on
    the capability-gated v1 API, so a consuming service had to either hand a nightly cron job an
    admin token or purge without checking. Both are bad, and one of them deletes evidence.

    Guarded by the internal token like consent-check, precisely so a sweeper does not need
    `compliance.manage`. A retention job should be able to ask "may I delete this" without also
    being able to place and release holds.

    Returns hold_ids, not just a bool, so the caller can NAME what stopped it in its own receipt.
    """
    return await build_compliance(request).hold_check(tenant=tenant, subject=subject)


@router.post("/compliance/dsar/{dsar_id}/target-ack", dependencies=[Depends(require_internal_token)])
async def target_ack(request: Request, dsar_id: str, body: AckIn) -> dict:
    """A service confirms it erased/exported its slice. Completion is derived from these."""
    res = await build_dsar(request).ack_target(dsar_id, body.service, body.category,
                                               ack=body.ack)
    if not res["ok"]:
        # A REFUSAL MUST NEVER WEAR A SUCCESS CODE. If this returned 200 with {"ok": false},
        # raise_for_status() would pass, the caller would ack the job, the target would never
        # flip, and the DSAR would hang for ever while every log line said OK — a false "she was
        # erased", which is the worst lie this service could tell. It has always raised; this
        # only sharpens WHICH refusal it is.
        #
        #   NOT_FOUND      -> 404  the DSAR does not exist
        #   UNKNOWN_TARGET -> 409  the DSAR exists, but this service/category is not one of its
        #                          targets. That is a CONFLICT with the job's state, not a
        #                          malformed request — 400 told the caller to fix its syntax when
        #                          the syntax was fine.
        s = get_settings()
        code = (s.COMPLIANCE_ACK_NOTFOUND_STATUS if res["reason"] == "NOT_FOUND"
                else s.COMPLIANCE_ACK_REJECT_STATUS)
        raise HTTPException(code, res["reason"])
    # The ack is another service ASSERTING it destroyed personal data, and DSAR completion is
    # derived from these — so this is the state transition that closes a DSAR, and it was the one
    # with no record behind it. "Who told us this target had erased its copy" was unanswerable.
    #
    # The actor is the SERVICE, not a person: this is an internal call and there is no human on
    # it. Naming the caller is the whole value of the entry, so it is recorded as `service:<name>`
    # rather than left empty — the empty-actor rule exists because a nameless entry proves
    # nothing, and that reasoning applies to machines too.
    #
    # Recorded only on the success path. An ack we REFUSED (unknown target, no such DSAR) did not
    # change anything, and logging it as though a target had acked would be the false completion
    # this endpoint's own comment exists to prevent.
    await request.app.state.auditor.record(
        "dsar.target_acked", actor=f"service:{body.service}", tenant=res.get("tenant", ""),
        resource=f"dsar:{dsar_id}", ip=request.client.host if request.client else "",
        metadata={"service": body.service, "category": body.category, "ack": body.ack})
    return res


@router.post("/compliance/dsar/run", dependencies=[Depends(require_internal_token)])
async def run_dsar_sweep(request: Request) -> dict:
    """Drive every open DSAR's unacked targets. Called by the erasure CronJob.

    This is the worker that makes an erasure an EFFECT rather than a record. Safe to call at any
    time and from any replica: acked targets are skipped and backoff suppresses duplicate dials, so
    an overlapping run costs a no-op. The response names anything STUCK — a target that exhausted
    its attempts and is holding a DSAR open — because a jammed erasure nobody can see is how an
    SLA breach becomes a regulatory one.
    """
    return await build_sweeper(request).run()


@router.post("/compliance/consent/sweep-lapses", dependencies=[Depends(require_internal_token)])
async def run_consent_sweep(request: Request) -> dict:
    """Announce guardian consents crossing the age of majority. Called by a CronJob.

    Until this existed, majority handling was one line in capacity.py — expires_at is set to the
    day the subject can speak for themself — and nothing watched that date. On their eighteenth
    birthday, check() simply started answering "expired": no notice to the controller, no prompt
    for the young adult's OWN consent. Processing stopped and from outside it looked like a bug.

    A lapse is not an error that surfaces itself; it is a date arriving, and a date arriving is
    only visible to something that looks.
    """
    await request.app.state.auditor.record(
        "consent.lapse_swept", actor="system:cron", tenant="", resource="consent",
        ip=request.client.host if request.client else "")
    return await build_consent_sweeper(request).run()


@router.post("/compliance/retention/run", dependencies=[Depends(require_internal_token)])
async def run_retention(request: Request) -> dict:
    """Purge records past their declared retention period. Called by the retention CronJob.

    Enforces the periods for the records THIS service owns; every other service enforces its own
    and declares it in its RoPA row. A legal hold beats retention exactly as it beats erasure, and
    a collection with no configured period is never purged — an undecided retention reads as KEEP.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "retention.purged", actor="", tenant="", resource="compliance_records",
        ip=request.client.host if request.client else "")
    return await build_retention(request).run()


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
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{body.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = ComplianceErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
