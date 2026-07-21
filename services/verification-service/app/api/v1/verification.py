"""verification-service — user + admin tier.

The split is by who is being trusted:

  A USER asserts something about themselves and asks us to check it. They may see their own
  verdicts and nobody else's.

  An ADMIN works the review queue — the highest-privilege operation here, because a reviewer can
  grant trust to anyone where an automated verifier can only repeat what a registry said.

One rule runs through every route: **the raw claim value goes in and never comes back.** It reaches
the provider and the hash, and is never stored, never logged, never returned. `public()` on the
model drops `claim_ref` too, because that hash is stable across tenants and handing it out would
let a caller correlate the same person between them — the exact linkage the hashing exists to
contain.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_any_capability
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.review import ReviewRefused, ReviewService
from app.core.stores import IdentityRegistry, VerificationStore
from app.core.verification_service import UnsupportedClaim, VerificationService

router = APIRouter(prefix="/verification", tags=["verification"])

_REVIEW = require_any_capability("platform.verification", "verification.review")
_READ = require_any_capability("platform.verification", "verification.review",
                               "verification.read")


async def build_service(request: Request, tenant: str = "") -> VerificationService:
    """Verifiers come from the pool, not from the container's boot-time map: a credential an
    operator rotated must reach the next check without a redeploy."""
    c = request.app.state.container
    s = get_settings()
    return VerificationService(
        store=VerificationStore(c.datastore),
        verifiers=await request.app.state.provider_pool.get(tenant), clock=c.clock,
        registry=IdentityRegistry(c.datastore),
        pii_salt=s.PII_SALT, ttl_days=s.verify_ttl_days,
        pending_ttl=s.VERIFY_PENDING_TTL_SECONDS,
        auditor=request.app.state.auditor, event_bus=c.event_bus)


def build_review(request: Request) -> ReviewService:
    c = request.app.state.container
    s = get_settings()
    return ReviewService(store=VerificationStore(c.datastore), clock=c.clock,
                         ttl_days=s.verify_ttl_days, two_eyes=s.VERIFY_TWO_EYES,
                         auditor=request.app.state.auditor)


class VerifyIn(BaseModel):
    claim_type: str = Field(min_length=1, max_length=64)
    # The raw identifier. In memory only: it goes to the provider and into a salted hash, and is
    # never persisted or echoed. Length-capped so a body-limit bypass cannot smuggle a document
    # through a field meant for a registration number.
    value: str = Field(min_length=1, max_length=512)
    extra: dict = Field(default_factory=dict)


class DecideIn(BaseModel):
    approve: bool
    note: str = Field(default="", max_length=1024)
    document_id: str = Field(default="", max_length=256)


# ── user tier ────────────────────────────────────────────────────────────────
@router.post("/claims", status_code=201, dependencies=[Depends(rate_limit)])
async def submit_claim(request: Request, body: VerifyIn,
                       principal: Principal = Depends(require_user)) -> dict:
    """Check a claim about yourself. Rate-limited: verification calls cost money at the provider,
    and an unthrottled endpoint is both a bill and an enumeration oracle."""
    try:
        svc = await build_service(request, principal.tenant)
        record = await svc.verify(
            tenant=principal.tenant, subject=principal.subject,
            claim_type=body.claim_type, value=body.value, extra=body.extra)
    except UnsupportedClaim as exc:
        # 422, not 404: the request is well-formed and the caller is authorised — this platform
        # simply does not verify that kind of claim, and the caller needs to know which it is.
        raise HTTPException(422, str(exc)) from exc
    return record.public()


@router.get("/claims")
async def my_claims(request: Request, principal: Principal = Depends(require_user)) -> dict:
    """Every verdict about ME. Scoped to the caller's own subject — never a lookup tool."""
    store = VerificationStore(request.app.state.container.datastore)
    rows = await store.for_subject(principal.tenant, principal.subject)
    return {"claims": [r.public() for r in rows]}


@router.get("/status")
async def claim_status(request: Request, claim_type: str,
                       principal: Principal = Depends(require_user)) -> dict:
    """Is my claim currently good? `trusted` is computed against the clock, so an expired verdict
    reports as expired the moment it lapses rather than when a sweeper next runs."""
    svc = await build_service(request, principal.tenant)
    return await svc.status(principal.tenant, principal.subject, claim_type)


# ── admin tier ───────────────────────────────────────────────────────────────
@router.get("/review-queue", dependencies=[Depends(_READ)])
async def review_queue(request: Request, principal: Principal = Depends(_READ)) -> dict:
    """Claims waiting on a human, oldest first."""
    return {"queue": await build_review(request).queue(principal.tenant)}


@router.post("/review/{verification_id}", dependencies=[Depends(_REVIEW)])
async def decide_review(request: Request, verification_id: str, body: DecideIn,
                        principal: Principal = Depends(_REVIEW)) -> dict:
    """Approve or refuse a claim. The most privileged operation in this service."""
    try:
        record = await build_review(request).decide(
            verification_id, approve=body.approve, reviewer=principal.subject,
            note=body.note, document_id=body.document_id)
    except ReviewRefused as exc:
        # 409: the request is fine, the verification's STATE forbids it — already settled, or the
        # reviewer is the subject. A 400 would tell the caller to fix syntax that is not wrong.
        raise HTTPException(409, str(exc)) from exc
    return record.public()
