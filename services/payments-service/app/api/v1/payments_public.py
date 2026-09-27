"""Public payments endpoints — UNAUTHENTICATED by design, because the callers are not our services:

  * the customer's browser, following a pay-link — GET /api/v1/payments/pay/{id} → 302 to the shop's
    gateway hosted page. The redirector exists so the shared link is OURS (a stable, revocable token)
    rather than the gateway URL, and so the target host is allow-listed (never an open redirect).
  * the shop's gateway, POSTing a status webhook — POST /api/v1/payments/webhook/{tenant}. There is
    no token to check here (the gateway has none of ours); the ADAPTER verifies the callback
    (signature or verify-by-fetch) and an unverified one changes nothing. Application is idempotent.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from starlette.responses import JSONResponse, RedirectResponse

from app.api.payments_deps import build_payment_service
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.payments import PaymentsDisabled

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


@router.get("/pay/{request_id}", dependencies=[Depends(rate_limit)])
async def pay_redirect(request: Request, request_id: str):
    """302 the customer to the gateway's hosted page for this request — or refuse (unknown, expired,
    spent, or a target host that is not allow-listed). NO EXISTENCE ORACLE (audit FIN-013): an unknown
    id and an existing-but-unusable one return the IDENTICAL response (404 + one generic body), so a
    link holder cannot tell 'never existed' from 'exists but spent/expired'. Only the happy path (a
    live, allow-listed link) differs — it 302s. Never redirects to an unvetted host."""
    svc = build_payment_service(request)
    try:
        target = await svc.redirect_target(request_id)
    except (KeyError, PaymentsDisabled):
        # One indistinguishable answer for BOTH unknown and unusable — the whole point of closing the
        # oracle. The body is generic and never names the concrete state (spent/expired/host).
        return JSONResponse({"ok": False, "reason": "payment link is not available"}, status_code=404)
    return RedirectResponse(target, status_code=302)


@router.post("/webhook/{tenant}", dependencies=[Depends(rate_limit)])
async def gateway_webhook(request: Request, tenant: str):
    """Receive a shop gateway's status webhook. The tenant in the path selects whose gateway verifies
    it; verification (signature / verify-by-fetch) is the gate, and application is idempotent, so a
    forged or replayed POST changes nothing. Always 200 to a well-formed request so the gateway does
    not retry-storm; the body says whether anything was applied."""
    # Body-size cap (H5): refuse an oversized payload BEFORE reading it into memory. A status webhook
    # is a few KB; anything larger is an unauthenticated memory-pressure attempt.
    max_bytes = get_settings().PAYMENTS_MAX_WEBHOOK_BYTES
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > max_bytes:
        return JSONResponse({"ok": False, "reason": "payload too large"}, status_code=413)
    body = await request.body()
    if len(body) > max_bytes:
        return JSONResponse({"ok": False, "reason": "payload too large"}, status_code=413)
    headers = {k.lower(): v for k, v in request.headers.items()}
    svc = build_payment_service(request)
    try:
        row = await svc.handle_webhook(tenant=tenant, headers=headers, body=body)
    except Exception:   # noqa: BLE001 — a malformed webhook must not 500 the gateway into retries
        # Log before swallowing (M8): keep the anti-retry-storm 200, but never hide a real bug.
        logging.getLogger("app.payments").exception(
            "gateway webhook processing failed for tenant %s", tenant)
        return JSONResponse({"ok": True, "applied": False})
    if row is None:
        return JSONResponse({"ok": True, "applied": False})
    return JSONResponse({"ok": True, "applied": True, "status": row.get("status")})
