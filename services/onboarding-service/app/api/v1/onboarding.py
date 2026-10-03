"""onboarding-service — user + admin tier.

The user endpoint answers one question: **what is still unsatisfied?** Never "which step am I on",
because there is no step. That single choice is what makes a UI free to render a checklist, a
wizard, or one blocking screen without this service caring, and what makes resume-after-abandonment
cost nothing.

The admin endpoints exist for the cases self-serve cannot reach — a customer stuck behind a
provider outage, a gift that failed to deliver. Both are privileged: force-completing a flow is
granting by hand what the requirements exist to check, so it is audited as critical and named for
what it is rather than dressed up as "fix onboarding".
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.evaluator import Evaluator
from app.core.flows import pick_flow, validate
from app.core.grants import GrantService
from app.core.provisioning import ProvisioningService
from app.onboarding_flows import ONBOARDING_FLOWS
from app.core.pii import subject_ref

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

_ADMIN = require_any_capability("platform.onboarding", "onboarding.manage")


def _flows() -> list:
    # Validated at boot too; re-validated here so a hot-reloaded declaration cannot serve a
    # malformed flow to a live customer.
    return validate(ONBOARDING_FLOWS)


def build_evaluator(request: Request) -> Evaluator:
    c = request.app.state.container
    return Evaluator(sources=c.requirement_sources, local_facts=c.local_facts)


def build_grants(request: Request) -> GrantService:
    c = request.app.state.container
    s = get_settings()
    return GrantService(entitlement=c.entitlement, ledger=c.grant_ledger,
                        identity_lookup=c.identity_lookup, specs=s.onboarding_grants,
                        local_facts=c.local_facts, auditor=request.app.state.auditor)


class ProfileIn(BaseModel):
    """What decides WHICH flow applies. Free-form on purpose: a platform's audiences are its own,
    and `account_type` / `joined_by` are examples rather than a schema."""
    profile: dict = Field(default_factory=dict)


class ForceIn(BaseModel):
    requirement: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)
    # onboarding-008: optional override of the configured force TTL (seconds). Omitted → the default bound.
    ttl_seconds: int | None = Field(default=None, ge=0)


# ── user tier ────────────────────────────────────────────────────────────────
@router.post("/next", dependencies=[Depends(rate_limit)])
async def whats_next(request: Request, body: ProfileIn,
                     principal: Principal = Depends(require_user)) -> dict:
    """What is still unsatisfied for me, right now.

    POST rather than GET because the audience profile decides which flow applies and belongs in a
    body, not in a query string that lands in access logs.

    Evaluated live on every call: a verification that expired since the last request shows as
    unsatisfied again, and anything it blocked is blocked again. Nothing here is remembered.
    """
    flow = pick_flow(_flows(), body.profile)
    if flow is None:
        # Refused rather than defaulted. Guessing a flow would put someone through the wrong
        # requirements — asking a personal account for a commercial registration, or letting a
        # business skip verification and collect a gift.
        raise HTTPException(422, "no onboarding flow matches this profile")
    state = await build_evaluator(request).evaluate(
        flow, tenant=principal.tenant, subject=principal.subject)
    return state.to_dict()


@router.post("/claim-grant", dependencies=[Depends(rate_limit)])
async def claim_grant(request: Request, body: ProfileIn,
                      principal: Principal = Depends(require_user)) -> dict:
    """Issue any grant whose requirement is now satisfied and unblocked.

    Idempotent by construction: the grant service keys on a verified identity, so calling this
    repeatedly — or racing it — yields one grant.
    """
    flow = pick_flow(_flows(), body.profile)
    if flow is None:
        raise HTTPException(422, "no onboarding flow matches this profile")

    state = await build_evaluator(request).evaluate(
        flow, tenant=principal.tenant, subject=principal.subject)
    by_id = {r["id"]: r for r in flow["requirements"]}
    grants = build_grants(request)

    issued = []
    for req_state in state.requirements:
        spec = by_id.get(req_state.id, {})
        if not spec.get("grants") or req_state.blocked_by:
            continue
        outcome = await grants.issue(
            kind=spec["grants"], once_per=spec.get("once_per", ""),
            tenant=principal.tenant, subject=principal.subject,
            requirement=req_state.id, scope=spec.get("scope", "tenant"))
        issued.append({"requirement": req_state.id, "grant": spec["grants"],
                       "status": outcome.status, "detail": outcome.detail})
    return {"issued": issued}


@router.post("/provision", dependencies=[Depends(rate_limit)])
async def provision(request: Request, body: ProfileIn,
                    principal: Principal = Depends(require_user)) -> dict:
    """Create what this flow says signup creates — atomically, or not at all.

    Safe to call twice: every step carries a key derived from the tenant, subject and step name, so
    a customer who times out and presses the button again gets the same tenant rather than a
    second one. If a later step fails, the earlier ones are undone — the orphan-tenant failure this
    was built to prevent.
    """
    flow = pick_flow(_flows(), body.profile)
    if flow is None:
        raise HTTPException(422, "no onboarding flow matches this profile")
    steps = flow.get("provisions") or []
    if not steps:
        return {"ok": True, "completed": [], "detail": "this flow provisions nothing"}

    c = request.app.state.container
    svc = ProvisioningService(provisioners=c.provisioners, store=c.datastore, clock=c.clock,
                              auditor=request.app.state.auditor, telemetry=c.telemetry)
    result = await svc.provision(tenant=principal.tenant, subject=principal.subject, steps=steps)
    if not result["ok"]:
        # 502: the request was fine and WE could not complete it against a downstream service. The
        # body names what was rolled back and what the world is still carrying, because an operator
        # needs that more than the customer needs a tidy message.
        raise HTTPException(502, {"error": "PROVISIONING_FAILED", **{
            k: result[k] for k in ("failed_step", "compensated", "residue", "unfinished")}})
    return result


# ── admin tier ───────────────────────────────────────────────────────────────
@router.post("/admin/inspect", dependencies=[Depends(_ADMIN)])
async def inspect(request: Request, subject: str, body: ProfileIn,
                  principal: Principal = Depends(_ADMIN)) -> dict:
    """See someone's flow as it stands — for supporting a customer who is stuck."""
    flow = pick_flow(_flows(), body.profile)
    if flow is None:
        raise HTTPException(422, "no onboarding flow matches this profile")
    state = await build_evaluator(request).evaluate(
        flow, tenant=principal.tenant, subject=subject)
    return state.to_dict()


@router.post("/admin/force/{subject}", dependencies=[Depends(_ADMIN)])
async def force_complete(request: Request, subject: str, body: ForceIn,
                         principal: Principal = Depends(_ADMIN)) -> dict:
    """Mark a requirement satisfied by hand.

    This is granting by hand exactly what the requirement exists to verify, so it is critical and
    audited, and the reason is REQUIRED — an override with no stated reason is indistinguishable
    from an attacker with an operator token.
    """
    await request.app.state.auditor.record(
        "flow.force_completed", actor=principal.subject, tenant=principal.tenant,
        resource=f"subject:{subject_ref(get_settings().PII_SALT, principal.tenant, subject)}",
        metadata={"requirement": body.requirement, "reason": body.reason})
    ledger = request.app.state.container.local_facts
    expires_at = await ledger.force(tenant=principal.tenant, subject=subject,
                                    requirement=body.requirement, actor=principal.subject,
                                    reason=body.reason, ttl_seconds=getattr(body, "ttl_seconds", None))
    return {"forced": body.requirement, "subject": subject, "expires_at": expires_at}


@router.delete("/admin/force/{subject}", dependencies=[Depends(_ADMIN)])
async def unforce_complete(request: Request, subject: str, requirement: str,
                           principal: Principal = Depends(_ADMIN)) -> dict:
    """Remove a hand-force so the live check resumes immediately (onboarding-008). Audited like the
    force it reverses — un-forcing is also a change to who is satisfying a requirement by hand."""
    await request.app.state.auditor.record(
        "flow.force_removed", actor=principal.subject, tenant=principal.tenant,
        resource=f"subject:{subject_ref(get_settings().PII_SALT, principal.tenant, subject)}",
        metadata={"requirement": requirement})
    ledger = request.app.state.container.local_facts
    removed = await ledger.unforce(tenant=principal.tenant, subject=subject, requirement=requirement)
    return {"unforced": requirement, "subject": subject, "existed": removed}


@router.get("/admin/flows", dependencies=[Depends(_ADMIN)])
async def list_flows(request: Request, principal: Principal = Depends(_ADMIN)) -> dict:
    """The flow catalogue — which onboarding flows exist, and each flow's requirements and the
    resources it provisions. The see-before-you-act read: an operator inspecting a stuck customer
    needs to know which flow they are on and what it demands, without reading the deployment config."""
    flows = _flows()
    # Read the ACTUAL declaration keys (onboarding-004): a flow's name is 'flow', its match is
    # 'audience', and a requirement's id is 'id'. The old code read 'name'/'match'/'name' — keys that
    # do not exist — so every flow came back {name: None, match: {}, requirements: [None, ...]}.
    return {"flows": [
        {"name": f.get("flow"), "match": f.get("audience", {}),
         "requirements": [r.get("id") for r in (f.get("requirements") or [])],
         "provisions": [p.get("kind") for p in (f.get("provisions") or [])]}
        for f in flows]}


@router.get("/admin/provisioning/{subject}", dependencies=[Depends(_ADMIN)])
async def provisioning_run(request: Request, subject: str,
                           principal: Principal = Depends(_ADMIN)) -> dict:
    """A customer's last provisioning run — what completed, what was rolled back, and the residue /
    unfinished cleanup still outstanding. The read an operator looks at before deciding to resume."""
    c = request.app.state.container
    key = ProvisioningService._run_key(principal.tenant, subject)
    record = await c.datastore.get("provisioning_runs", key)
    if record is None:
        # A stable, honest empty — 'never provisioned' and 'provisioned cleanly, nothing stored'
        # both read as no outstanding work, which is what the operator needs to know.
        return {"subject": subject, "found": False, "unfinished": [], "residue": []}
    return {"subject": subject, "found": True, **record}


@router.post("/admin/resume/{subject}", dependencies=[Depends(_ADMIN)])
async def resume_provisioning(request: Request, subject: str,
                              principal: Principal = Depends(_ADMIN)) -> dict:
    """Re-attempt the compensation a failed provisioning could not finish for this customer.

    Audited: it re-runs undo work against downstream services on a customer's behalf. It never
    re-runs the forward steps (those succeeded), so it is safe to call repeatedly — it works the
    'unfinished' queue and returns what is left. Nothing to resume is a clean, recorded no-op.
    """
    c = request.app.state.container
    svc = ProvisioningService(provisioners=c.provisioners, store=c.datastore, clock=c.clock,
                              auditor=request.app.state.auditor, telemetry=c.telemetry)
    await request.app.state.auditor.record(
        "provisioning.resumed", actor=principal.subject, tenant=principal.tenant,
        resource=f"subject:{subject_ref(get_settings().PII_SALT, principal.tenant, subject)}")
    result = await svc.resume(principal.tenant, subject)
    if result is None:
        return {"subject": subject, "resumed": False, "detail": "nothing outstanding to resume"}
    return {"subject": subject, "resumed": True, **result}
