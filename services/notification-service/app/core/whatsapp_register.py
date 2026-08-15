"""Register a business phone number for Cloud API sending — the PIN-carrying step exchange stops before.

Embedded Signup (whatsapp_onboarding) stores the token, but Meta refuses to SEND from a number until
it is REGISTERED: POST /{phone_number_id}/register with the business's 6-digit two-step-verification
PIN. Without this a tenant is "connected" on every screen and cannot send a single message — every
layer reports success and only the customer who never receives anything knows.

The token and phone id are read from the same per-tenant credentials ProviderPool holds, so the caller
supplies ONLY the PIN the business chose. And the three failures are three sentences: a mistyped PIN
(WRONG_PIN), the 10-attempts-per-72h lock Meta answers with error 133016 (RATE_LIMITED), and not
reaching Meta at all (UNREACHABLE) must never arrive as one indistinguishable 400.

Codes verified against Meta's Cloud API registration docs (Graph v21, developers.facebook.com).
"""
from __future__ import annotations

import json as _json
from dataclasses import dataclass

from app.adapters.notify_http import bearer, ok, post_json

# Two-step-verification PIN mismatch — the business typed the wrong 6 digits.
_WRONG_PIN_CODES = {133005}
# Too many attempts: Meta locks the number after 10 registration attempts per 72h and answers 133016.
_RATE_LIMITED_CODES = {133016}


@dataclass(frozen=True)
class RegisterResult:
    registered: bool
    # "" on success; else one of WRONG_PIN | RATE_LIMITED | UNREACHABLE | META_ERROR[: msg]. NOT_CONNECTED
    # is decided by the caller (no stored token/phone), never reached here.
    reason: str = ""


def _classify(status: int, body: str) -> str:
    """Turn Meta's HTTP status + error envelope into ONE of the distinct, caller-safe reasons."""
    if status == 0:
        # post_json returns (0, "request failed") when Meta could not be reached at all — a network,
        # DNS or TLS failure is "we could not reach Meta", never "wrong PIN".
        return "UNREACHABLE"
    code = None
    msg = ""
    try:
        err = _json.loads(body).get("error", {}) or {}
        code = err.get("code")
        msg = str(err.get("message", "") or "")
    except Exception:  # noqa: BLE001 — a non-JSON error body is still a Meta rejection, just unlabelled
        pass
    if code in _RATE_LIMITED_CODES:
        return "RATE_LIMITED"
    if code in _WRONG_PIN_CODES:
        return "WRONG_PIN"
    # A real but unclassified Meta rejection: surface its message so the owner is not left guessing,
    # but keep it clearly distinct from the three named outcomes above.
    return f"META_ERROR: {msg}" if msg else "META_ERROR"


def register_number(*, access_token: str, phone_number_id: str, pin: str,
                    graph_base: str) -> RegisterResult:
    """Blocking Graph POST — call via asyncio.to_thread. Returns registered, or a DISTINCT reason.

    The PIN is assumed already validated as 6 digits by the caller, so a malformed PIN never spends
    one of the 10 attempts Meta allows before it locks the number.
    """
    base = (graph_base or "https://graph.facebook.com/v21.0").rstrip("/")
    status, body = post_json(
        f"{base}/{phone_number_id}/register",
        {"messaging_product": "whatsapp", "pin": pin}, bearer(access_token))
    if ok(status):
        return RegisterResult(True)
    return RegisterResult(False, _classify(status, body))
