# =============================================================================
# assistant-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from pydantic import BaseModel, Field

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.config import Settings, get_settings
from app.core.pii import subject_ref
from app.core.erasure import TemplateErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])

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


@router.post("/assistant/retention", dependencies=[Depends(require_internal_token)])
async def enforce_retention(request: Request) -> dict:
    """Apply CONVERSATION_RETENTION_DAYS (a scheduler calls this). 0 = keep until erased.

    Shipped with a cron caller in the chart, because a sweeper nobody calls is the same defect as
    no sweeper at all — and harder to spot, since the code review passes.
    """
    import datetime as _dt

    s = get_settings()
    store = request.app.state.container.conversations
    if store is None or s.CONVERSATION_RETENTION_DAYS <= 0:
        return {"conversations": 0, "messages": 0, "skipped": True}
    cutoff = (_dt.datetime.now(_dt.timezone.utc)
              - _dt.timedelta(days=s.CONVERSATION_RETENTION_DAYS)).isoformat()
    return await store.enforce_retention(cutoff_iso=cutoff)


class TriageIn(BaseModel):
    tenant: str = ""
    current_type: str = ""
    subject: str = ""
    body: str = ""
    candidate_types: list[str] = Field(default_factory=list)


class DraftIn(BaseModel):
    tenant: str = ""
    subject: str = ""
    thread: list[dict] = Field(default_factory=list)


@router.post("/assistant/triage", dependencies=[Depends(require_internal_token)])
async def internal_triage(request: Request, body: TriageIn) -> dict:
    """Customer-service AI: suggest a type/priority/tags/summary for a new ticket. ADVISORY — an
    empty object means 'no suggestion' (no generative model wired, or nothing usable back), which the
    caller (ticket-service) treats as a shrug. Enabled only when MODEL_DRIVER is a real LLM."""
    from app.core import support
    return await support.triage(
        request.app.state.container.model, current_type=body.current_type, subject=body.subject,
        body=body.body, candidate_types=tuple(body.candidate_types),
        lang=get_settings().DEFAULT_LANGUAGE)


@router.post("/assistant/draft", dependencies=[Depends(require_internal_token)])
async def internal_draft(request: Request, body: DraftIn) -> dict:
    """Customer-service AI: draft a reply an agent reviews before sending. ADVISORY (see triage)."""
    from app.core import support
    return await support.draft(
        request.app.state.container.model, subject=body.subject, thread=body.thread,
        lang=get_settings().DEFAULT_LANGUAGE)


@router.post("/privacy/erase", dependencies=[Depends(require_internal_token)])
async def erase_subject(request: Request, body: EraseIn) -> dict:
    """Erase a data subject. Called by compliance-service during a DSAR fan-out.

    Returns the ack: what was erased, and what was RETAINED with its legal basis. Idempotent — a
    DSAR retries, so erasing an already-erased subject must not fail. Wire this service into the
    fan-out by adding it to PRIVACY_TARGETS in compliance-service's values file; the
    safety:privacy-wiring gate fails if you implement this and forget.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all, so this runs
    # BEFORE the mutation. Recording afterwards would 503 a caller whose erasure had already
    # succeeded, and leave the one irreversible action in the service unproven.
    #
    # The resource carries the PSEUDONYM rather than the subject: an erasure record that names the
    # person it erased defeats the erasure.
    ref = subject_ref(get_settings().PII_SALT, body.tenant, body.subject)
    await request.app.state.auditor.record(
        "subject.erased", actor="", tenant=body.tenant, resource=f"subject:{ref}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    s = get_settings()
    eraser = TemplateErasure(store=c.datastore, pii_salt=s.PII_SALT,
                             max_scan=s.ERASURE_MAX_SCAN)
    ack = await eraser.erase(body.tenant, body.subject)
    # Conversations are this service's own personal data, so the ack must account for them or it
    # claims an erasure it did not perform.
    if c.conversations is not None:
        removed = await c.conversations.erase_subject(tenant=body.tenant, subject=body.subject)
        erased = list(ack.get("erased") or [])
        if removed["conversations"]:
            erased.append(f"conversations:{removed['conversations']}")
        if removed["messages"]:
            erased.append(f"messages:{removed['messages']}")
        ack["erased"] = erased
        ack["already_absent"] = ack.get("already_absent", True) and not erased
    return ack
