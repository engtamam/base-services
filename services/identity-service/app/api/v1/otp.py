# =============================================================================
# identity-service — OTP routes (/api/v1/otp)
# -----------------------------------------------------------------------------
# request → mint a code, deliver it over notification-service; verify → consume it.
# The code NEVER appears in a response body: the only way to learn it is to receive
# the message, which is the whole point of proving control of the channel.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status, Depends
from pydantic import BaseModel, Field

from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.otp_service import OtpError
from app.observability.logging import get_logger
from app.ports.errors import ValidationError

router = APIRouter(prefix="/api/v1/otp", tags=["otp"])
_log = get_logger(__name__)

# A closed set — a caller must not invent purposes that share a namespace with, say, a login code.
_PURPOSES = {"verify", "login", "reset"}


class OtpRequestIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=256)
    purpose: str = Field(default="verify", max_length=32)


class OtpVerifyIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=32)
    purpose: str = Field(default="verify", max_length=32)


def _purpose(raw: str) -> str:
    p = (raw or "verify").strip().lower()
    if p not in _PURPOSES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "unknown otp purpose")
    return p


def _canonical(request: Request, identifier: str) -> str:
    """Normalize with the login identifier so every spelling of a phone (0501…, +96650…) proves the
    SAME code — uniqueness must survive normalization here too. If this deployment's login field
    can't parse it (e.g. login is username but the OTP targets an email), fall back to the trimmed
    raw value rather than refuse."""
    ident = request.app.state.container.login_identifier
    try:
        ident.validate(identifier)
        return ident.normalize(identifier)
    except ValidationError:
        return identifier.strip()


@router.post("/request", dependencies=[Depends(rate_limit)])
async def request_otp(body: OtpRequestIn, request: Request) -> dict:
    """Issue a code and hand it to notification-service. The response is the SAME whether or not
    delivery succeeded and whether or not the identifier is known — it reveals only that a code was
    requested, never that an account exists (no enumeration oracle)."""
    s = get_settings()
    purpose = _purpose(body.purpose)
    otp = request.app.state.container.otp_service
    key = _canonical(request, body.identifier)
    try:
        issued = await otp.issue(identifier=key, purpose=purpose)
    except OtpError as e:
        if e.code in ("RESEND_TOO_SOON", "RATE_LIMITED"):
            # A real limit the client should back off on — the one honest 429.
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "please wait before requesting another code")
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "could not issue a code")

    delivered = False
    if s.OTP_TEMPLATE:
        variables = {"code": issued["code"], "expires_in": issued["expires_in"], "purpose": purpose}
        if s.OTP_DELIVERY_CHANNEL:
            variables["channel"] = s.OTP_DELIVERY_CHANNEL      # still exposed for template substitution
        # Never fatal: a provider hiccup must not 500. The human can request another code.
        # OTP_DELIVERY_CHANNEL is the REAL channel selector now (was only a template variable, so a
        # phone OTP silently went out as email). Empty falls back to email — the prior behaviour.
        delivered = await request.app.state.container.notifier.send(
            to=body.identifier, template=s.OTP_TEMPLATE, tenant=s.OTP_NOTIFY_TENANT,
            variables=variables, channel=s.OTP_DELIVERY_CHANNEL or "email")
    return {"requested": True, "expires_in": issued["expires_in"], "delivered": bool(delivered)}


@router.post("/verify", dependencies=[Depends(rate_limit)])
async def verify_otp(body: OtpVerifyIn, request: Request) -> dict:
    """Consume a code. Every failure — never issued, expired, wrong, exhausted — collapses to ONE
    generic 400 so a caller cannot distinguish 'wrong code' from 'no code for this identifier'."""
    purpose = _purpose(body.purpose)
    otp = request.app.state.container.otp_service
    key = _canonical(request, body.identifier)
    try:
        result = await otp.verify(identifier=key, code=body.code, purpose=purpose)
    except OtpError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid or expired code")
    return {"verified": True, "purpose": result["purpose"]}
