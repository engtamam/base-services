"""Public payments endpoints — UNAUTHENTICATED by design, because the callers are not our services:

  * the customer's browser, following a pay-link — GET /api/v1/payments/pay/{id} → 302 to the shop's
    gateway hosted page. The redirector exists so the shared link is OURS (a stable, revocable token)
    rather than the gateway URL, and so the target host is allow-listed (never an open redirect).
  * the shop's gateway, POSTing a status webhook — POST /api/v1/payments/webhook/{tenant}. There is
    no token to check here (the gateway has none of ours); the ADAPTER verifies the callback
    (signature or verify-by-fetch) and an unverified one changes nothing. Application is idempotent.
"""
from __future__ import annotations

from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, RedirectResponse

from app.api.payments_deps import build_payment_service
from app.core.payments import PaymentsDisabled

router = APIRouter(prefix="/api/v1/payments", tags=["payments"])


@router.get("/pay/{request_id}")
async def pay_redirect(request: Request, request_id: str):
    """302 the customer to the gateway's hosted page for this request — or refuse (expired, spent,
    or a target host that is not allow-listed). Never reveals whether an id exists beyond a generic
    404, and never redirects to an unvetted host."""
    svc = build_payment_service(request)
    try:
        target = await svc.redirect_target(request_id)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "not found"}, status_code=404)
    except PaymentsDisabled as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, status_code=410)
    return RedirectResponse(target, status_code=302)


@router.post("/webhook/{tenant}")
async def gateway_webhook(request: Request, tenant: str):
    """Receive a shop gateway's status webhook. The tenant in the path selects whose gateway verifies
    it; verification (signature / verify-by-fetch) is the gate, and application is idempotent, so a
    forged or replayed POST changes nothing. Always 200 to a well-formed request so the gateway does
    not retry-storm; the body says whether anything was applied."""
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    svc = build_payment_service(request)
    try:
        row = await svc.handle_webhook(tenant=tenant, headers=headers, body=body)
    except Exception:   # noqa: BLE001 — a malformed webhook must not 500 the gateway into retries
        return JSONResponse({"ok": True, "applied": False})
    if row is None:
        return JSONResponse({"ok": True, "applied": False})
    return JSONResponse({"ok": True, "applied": True, "status": row.get("status")})
