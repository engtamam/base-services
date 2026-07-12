# =============================================================================
# notification-service — Internal tier (service-to-service, X-Internal-Token)
# -----------------------------------------------------------------------------
# NOT user-facing. Guarded by a shared token compared in CONSTANT TIME
# (CWE-208: observable timing discrepancy). Never a user JWT.
# =============================================================================
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.v1.notifications import build_notifier
from app.config import Settings, get_settings
from app.core.erasure import NotificationErasure
from app.manifest import build_manifest

router = APIRouter(prefix="/internal", tags=["internal"])


class NotifyIn(BaseModel):
    recipient: str = Field(min_length=1, max_length=256)
    channel: str = Field(pattern="^(email|sms|inapp)$")
    template_key: str = Field(min_length=1, max_length=128)
    locale: str | None = None
    variables: dict = Field(default_factory=dict)
    user_id: str | None = None
    event: str | None = None
    idempotency_key: str | None = Field(default=None, max_length=256)

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


@router.post("/notify", dependencies=[Depends(require_internal_token)])
async def internal_notify(request: Request, body: NotifyIn) -> dict:
    """Service-to-service send. Returns the delivery id + status."""
    return await build_notifier(request).notify(
        recipient=body.recipient, channel=body.channel, template_key=body.template_key,
        locale=body.locale, variables=body.variables, user_id=body.user_id, event=body.event,
        idempotency_key=body.idempotency_key,
    )


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
    eraser = NotificationErasure(store=c.datastore, pii_salt=s.PII_SALT)
    return await eraser.erase(body.tenant, body.subject)
