"""WhatsApp Business management reads/writes an operator needs — beyond send + register.

These call Meta's Graph as the business (token read in core, never in a route), off the event loop.
Verified against Meta's current Cloud API / Graph docs (2026-09-10):

  * SET/ROTATE two-step-verification PIN — POST /{PHONE_NUMBER_ID} with {"pin": "<6 digits>"}. This is
    DISTINCT from registration (POST /{phone_number_id}/register, which ACTIVATES sending using the
    PIN): this sets the PIN itself. Write-only — the PIN is never read back by any route.
  * READ subscribed apps/fields — GET /{WABA_ID}/subscribed_apps returns the apps subscribed to the
    WABA's webhooks and, per app, the fields they receive. (Meta does NOT expose a per-WABA WRITE of
    subscribed_fields — that is configured at the APP level in the App Dashboard / app subscriptions —
    so this is read-only: it lets an operator CONFIRM what Meta is actually sending, which is the gap.)
  * READ business verification — GET /{WABA_ID}?fields=business_verification_status,status. The field
    is `business_verification_status` (verified | pending | rejected | …); there is no
    `account_review_status` on the WABA node. It gates template-review speed and throughput upgrades.
"""
from __future__ import annotations

import json as _json

from app.adapters.notify_http import bearer, get_json, ok, post_json
from app.core.whatsapp_register import _classify

_DEFAULT_BASE = "https://graph.facebook.com/v26.0"


def _base(graph_base: str) -> str:
    return (graph_base or _DEFAULT_BASE).rstrip("/")


def set_two_step_pin(*, access_token: str, phone_number_id: str, pin: str,
                     graph_base: str) -> tuple[bool, str]:
    """Set/rotate the 6-digit two-step-verification PIN. Blocking Graph POST — call via to_thread.

    Returns (ok, reason). The PIN is assumed already validated as 6 digits by the caller. Never
    returns or logs the PIN itself."""
    status, body = post_json(f"{_base(graph_base)}/{phone_number_id}", {"pin": pin},
                             bearer(access_token))
    return (True, "") if ok(status) else (False, _classify(status, body))


def fetch_subscribed_apps(*, access_token: str, waba_id: str, graph_base: str) -> dict:
    """GET /{WABA_ID}/subscribed_apps — which apps (and their subscribed webhook fields) the WABA has.

    Read-only (Meta has no per-WABA field write). {ok, apps:[...]} on success, else {ok:False, reason}
    in the same distinct vocabulary as register (UNREACHABLE / TOKEN_REVOKED / META_ERROR[:msg])."""
    status, body = get_json(f"{_base(graph_base)}/{waba_id}/subscribed_apps", {}, bearer(access_token))
    if not ok(status):
        return {"ok": False, "reason": _classify(status, body)}
    try:
        data = _json.loads(body)
    except ValueError:
        return {"ok": False, "reason": "META_ERROR"}
    return {"ok": True, "apps": data.get("data", []) or []}


def fetch_business_verification(*, access_token: str, waba_id: str, graph_base: str) -> dict:
    """GET /{WABA_ID}?fields=business_verification_status,status — the WABA's verification + status."""
    status, body = get_json(f"{_base(graph_base)}/{waba_id}",
                            {"fields": "business_verification_status,status"}, bearer(access_token))
    if not ok(status):
        return {"ok": False, "reason": _classify(status, body)}
    try:
        data = _json.loads(body)
    except ValueError:
        return {"ok": False, "reason": "META_ERROR"}
    return {"ok": True,
            "business_verification_status": str(data.get("business_verification_status", "") or ""),
            "status": str(data.get("status", "") or "")}


# ── webhook override (per phone / per WABA) — the Meta-side handover lever ─────────────────────────
# Verified against Meta docs 2026-09-19 (webhooks/override): a per-phone or per-WABA override routes
# that number's/WABA's message webhooks to a chosen endpoint instead of the app default — the lever
# that hands a number's ordinary chat to Meta Business Agent (or anywhere). Priority: phone → WABA →
# app default; account/template webhooks always go to the app default. SECURITY: this reroutes a
# shop's message delivery, so the caller (API layer) must gate it (internal token), audit it, and
# refuse a target host that is not allow-listed — never let it point a shop's messages anywhere.

def set_phone_webhook_override(*, access_token: str, phone_number_id: str, callback_uri: str,
                               verify_token: str, graph_base: str) -> tuple[bool, str]:
    """POST /{PHONE_NUMBER_ID} {"webhook_configuration":{override_callback_uri, verify_token}}.
    Blocking Graph POST — call via to_thread. Returns (ok, reason). Never logs/returns verify_token."""
    status, body = post_json(
        f"{_base(graph_base)}/{phone_number_id}",
        {"webhook_configuration": {"override_callback_uri": callback_uri, "verify_token": verify_token}},
        bearer(access_token))
    return (True, "") if ok(status) else (False, _classify(status, body))


def clear_phone_webhook_override(*, access_token: str, phone_number_id: str,
                                 graph_base: str) -> tuple[bool, str]:
    """POST /{PHONE_NUMBER_ID} {"webhook_configuration":{override_callback_uri:""}} — revert to the app
    default (the on→off / hand-back). Blocking Graph POST — call via to_thread."""
    status, body = post_json(
        f"{_base(graph_base)}/{phone_number_id}",
        {"webhook_configuration": {"override_callback_uri": ""}}, bearer(access_token))
    return (True, "") if ok(status) else (False, _classify(status, body))


def set_waba_webhook_override(*, access_token: str, waba_id: str, callback_uri: str,
                              verify_token: str, graph_base: str) -> tuple[bool, str]:
    """POST /{WABA_ID}/subscribed_apps {override_callback_uri, verify_token}. Blocking — via to_thread."""
    status, body = post_json(
        f"{_base(graph_base)}/{waba_id}/subscribed_apps",
        {"override_callback_uri": callback_uri, "verify_token": verify_token}, bearer(access_token))
    return (True, "") if ok(status) else (False, _classify(status, body))


def clear_waba_webhook_override(*, access_token: str, waba_id: str,
                                graph_base: str) -> tuple[bool, str]:
    """POST /{WABA_ID}/subscribed_apps with NO body — re-subscribe plainly, dropping the override."""
    status, body = post_json(
        f"{_base(graph_base)}/{waba_id}/subscribed_apps", {}, bearer(access_token))
    return (True, "") if ok(status) else (False, _classify(status, body))
