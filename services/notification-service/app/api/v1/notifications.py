# =============================================================================
# notification-service — user + admin routes
# =============================================================================
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_capability
from app.api.deps import get_guard
from app.config import get_settings
from app.core.models import Preference, Template
from app.core.notifier import NotificationService
from app.core.retry import RetryPolicy
from app.core.stores import (
    DeliveryStore, InAppStore, PreferenceStore, TemplateStore,
)

notifications_router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])
templates_router = APIRouter(prefix="/api/v1/templates", tags=["templates"])


def build_notifier(request: Request) -> NotificationService:
    c = request.app.state.container
    s = get_settings()
    store = c.datastore
    return NotificationService(
        templates=TemplateStore(store), prefs=PreferenceStore(store),
        deliveries=DeliveryStore(store),
        inapp=InAppStore(store, page_size=s.FEED_PAGE_SIZE, scan_limit=s.FEED_SCAN_LIMIT),
        email=c.email, sms=c.sms, renderer=c.renderer,
        email_from=s.EMAIL_FROM, default_locale=s.DEFAULT_LOCALE,
        dedupe_ttl_seconds=s.NOTIFY_DEDUPE_TTL_SECONDS,
        dedupe_states=s.notify_dedupe_states,
        delivery_mode=s.NOTIFY_DELIVERY_MODE,
        retry=RetryPolicy(
            max_attempts=s.NOTIFY_MAX_ATTEMPTS,
            base_ms=s.NOTIFY_BACKOFF_BASE_MS, max_ms=s.NOTIFY_BACKOFF_MAX_MS,
            retryable=s.notify_retryable, permanent=s.notify_permanent,
            unknown_is_retryable=s.NOTIFY_UNKNOWN_ERROR_RETRYABLE),
    )


# ── Schemas ─────────────────────────────────────────────────────────────────
class PreferenceIn(BaseModel):
    suppressed: bool = False
    opt_out_channels: list[str] = Field(default_factory=list)
    opt_out_events: list[str] = Field(default_factory=list)


class TemplateIn(BaseModel):
    key: str = Field(min_length=1, max_length=128)
    locale: str = Field(default="en", max_length=16)
    subject: str = Field(default="", max_length=512)
    body: str = Field(min_length=1, max_length=20000)
    version: int = 1


# ── User: in-app feed ────────────────────────────────────────────────────────
@notifications_router.get("")
async def feed(request: Request, principal: Principal = Depends(require_user),
               guard=Depends(get_guard)) -> dict:
    items = await build_notifier(request).feed(principal.subject)
    async with guard.step("respond") as step:
        body = {"items": items}
        step.result = body
    return body


@notifications_router.post("/{item_id}/read")
async def mark_read(request: Request, item_id: str,
                    principal: Principal = Depends(require_user)) -> dict:
    ok = await build_notifier(request).mark_read(principal.subject, item_id)
    if not ok:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return {"status": "ok"}


# ── User: preferences ────────────────────────────────────────────────────────
@notifications_router.get("/preferences")
async def get_preferences(request: Request, principal: Principal = Depends(require_user)) -> dict:
    pref = await PreferenceStore(request.app.state.container.datastore).get(principal.subject)
    return pref.to_dict()


@notifications_router.put("/preferences")
async def set_preferences(request: Request, body: PreferenceIn,
                          principal: Principal = Depends(require_user)) -> dict:
    pref = Preference(user=principal.subject, suppressed=body.suppressed,
                      opt_out_channels=body.opt_out_channels, opt_out_events=body.opt_out_events)
    await PreferenceStore(request.app.state.container.datastore).set(pref)
    return pref.to_dict()


# ── Admin: template management (capability-gated) ────────────────────────────
@templates_router.post("", dependencies=[Depends(require_capability("notifications.templates"))])
async def upsert_template(request: Request, body: TemplateIn) -> dict:
    tmpl = Template(key=body.key, locale=body.locale, subject=body.subject,
                    body=body.body, version=body.version)
    await TemplateStore(request.app.state.container.datastore).upsert(tmpl)
    return tmpl.to_dict()


@templates_router.get("", dependencies=[Depends(require_capability("notifications.templates"))])
async def list_templates(request: Request) -> dict:
    rows = await TemplateStore(request.app.state.container.datastore).list()
    return {"templates": rows}
