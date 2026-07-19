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
from app.onboarding_flows import ONBOARDING_FLOWS

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
                        auditor=request.app.state.auditor)


class ProfileIn(BaseModel):
    """What decides WHICH flow applies. Free-form on purpose: a platform's audiences are its own,
    and `account_type` / `joined_by` are examples rather than a schema."""
    profile: dict = Field(default_factory=dict)


class ForceIn(BaseModel):
    requirement: str = Field(min_length=1, max_length=128)
    reason: str = Field(min_length=1, max_length=512)


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
            tenant=principal.tenant, subject=principal.subject)
        issued.append({"requirement": req_state.id, "grant": spec["grants"],
                       "status": outcome.status, "detail": outcome.detail})
    return {"issued": issued}


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
        resource=f"subject:{subject}",
        metadata={"requirement": body.requirement, "reason": body.reason})
    ledger = request.app.state.container.local_facts
    await ledger.force(tenant=principal.tenant, subject=subject,
                       requirement=body.requirement, actor=principal.subject, reason=body.reason)
    return {"forced": body.requirement, "subject": subject}
