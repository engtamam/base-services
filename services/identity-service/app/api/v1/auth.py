# =============================================================================
# identity-service — auth routes (/api/v1/auth)
# -----------------------------------------------------------------------------
# register · login · refresh · logout · me · change-password · MFA enroll/verify
# Token-returning routes intentionally emit tokens; the /me profile is wrapped in
# the CWE respond-guard to guarantee it never leaks password_hash/mfa_secret.
# =============================================================================
from __future__ import annotations

import hashlib

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.authz import require_any_capability
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.observability.logging import get_logger
from app.core.auth_service import AuthService, SoleOwnerError, WeakPassword
from app.ports.errors import ValidationError
from app.core.login_attempts import LoginAttemptStore
from app.core.invites import InviteStore
from app.core.email_verification import VerificationStore
from app.core.membership import MembershipStore
from app.core.refresh_store import RefreshStore
from app.ports.caps_resolver import CapsUnavailable
from app.core.user_repository import UserExists, UserRepository

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Schemas ─────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    """What a stranger may state about themself: who they are and a password. Nothing else.

    `caps` and `roles` USED TO BE FIELDS HERE, and this route is unauthenticated. Anyone able to
    reach it could send caps=["*"] and receive a token that satisfies every capability check on
    the platform — the gateway honours "*" as a wildcard — for any tenant they named. That is a
    total compromise reachable by one POST, and REGISTRATION_ENABLED defaults to true.

    They are not validated here; they are ABSENT. Pydantic ignores unknown fields, so an existing
    client that still sends them keeps working and the values are discarded — a 400 would only
    tell an attacker which field to rename. Privilege comes from the server, never the request.

    core/tenancy.py already said this for the multi-tenant path: "a request may ASK for a tenant;
    it may never SUPPLY one", and "caps and roles come from the Membership". This legacy
    single-tenant route bypassed that guard entirely.
    """

    username: str = Field(min_length=3, max_length=128)
    # min length is enforced by the core policy (PASSWORD_MIN_LENGTH); max is a DoS cap.
    password: str = Field(min_length=1, max_length=256)
    tenant: str = Field(default="default", max_length=128)


class VerifyEmailIn(BaseModel):
    token: str = Field(min_length=1, max_length=512)


class ResendIn(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    tenant: str = Field(default="default", max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str
    tenant: str = "default"
    code: str | None = None  # MFA code when enrolled


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePwIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=1, max_length=256)  # core enforces min length


class ResetPwIn(BaseModel):
    identifier: str = Field(min_length=1, max_length=256)
    code: str = Field(min_length=1, max_length=32)
    new_password: str = Field(min_length=1, max_length=256)  # core enforces min length


class MfaVerifyIn(BaseModel):
    code: str


# ── Wiring ──────────────────────────────────────────────────────────────────
def _svc(request: Request) -> AuthService:
    c = request.app.state.container
    s = get_settings()
    return AuthService(
        users=UserRepository(c.datastore, c.login_identifier), refresh=RefreshStore(c.datastore),
        hasher=c.password_hasher, issuer=c.token_issuer, verifier=c.token_verifier,
        mfa=c.mfa, identifier=c.login_identifier,
        access_ttl=s.ACCESS_TTL_SECONDS, refresh_ttl=s.REFRESH_TTL_SECONDS,
        min_password_len=s.PASSWORD_MIN_LENGTH,
        lockout=LoginAttemptStore(c.datastore, max_attempts=s.LOGIN_MAX_ATTEMPTS,
                                  window_seconds=s.LOGIN_ATTEMPT_WINDOW_SECONDS,
                                  lockout_seconds=s.LOGIN_LOCKOUT_SECONDS),
        # Off (the default) => None => every path in AuthService behaves exactly as it did
        # before Phase 31. The multi-tenant routes 404 rather than half-exist.
        memberships=MembershipStore(c.datastore) if s.IDENTITY_MULTI_TENANT else None,
        invites=InviteStore(c.datastore) if s.IDENTITY_MULTI_TENANT else None,
        # Roles are the input; capabilities are derived. The resolver decides where from.
        caps_resolver=c.caps_resolver if s.IDENTITY_MULTI_TENANT else None,
        # platform.* is a BOUNDARY now, not a naming convention.
        platform_tenant=s.PLATFORM_TENANT,
        verifications=(VerificationStore(
            c.datastore, pepper=s.EMAIL_VERIFICATION_PEPPER,
            ttl_seconds=s.EMAIL_VERIFICATION_TTL_SECONDS,
            max_outstanding=s.EMAIL_VERIFICATION_MAX_OUTSTANDING)
            if s.EMAIL_VERIFICATION_ENABLED else None),
        require_verified_email=(s.EMAIL_VERIFICATION_ENABLED
                                and s.LOGIN_REQUIRES_VERIFIED_EMAIL),
        legacy_email_policy=s.EMAIL_VERIFICATION_LEGACY,
    )


# ── Routes ──────────────────────────────────────────────────────────────────
@router.get("/onboarding-methods")
async def onboarding_methods() -> dict:
    """Which onboarding methods this deployment has enabled — so a signup screen renders exactly the
    options that will work (a 'Create account' button only when open registration is on, an 'I have
    an invite' path only when invites are, and so on). Pre-auth by nature: the caller has no session
    yet, that is the whole point of onboarding."""
    from app.core.onboarding import build_onboarding_policy
    return {"methods": build_onboarding_policy(get_settings()).list_enabled()}


@router.post("/register", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit)])
async def register(request: Request, body: RegisterIn) -> dict:
    # Open self-service registration is one onboarding METHOD, gated by the authoritative policy.
    # With no explicit ONBOARDING_METHODS this asks REGISTRATION_ENABLED exactly as before.
    from app.core.onboarding import OPEN, OnboardingMethodDisabled, build_onboarding_policy
    try:
        build_onboarding_policy(get_settings()).require(OPEN)
    except OnboardingMethodDisabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "REGISTRATION_DISABLED")
    svc = _svc(request)
    try:
        # Roles come from CONFIG, not the caller. A platform that wants self-service signup to
        # grant something states it once, in one place an operator reviews, rather than trusting
        # whatever arrives in a body.
        s = get_settings()
        user = await svc.register(body.tenant, body.username, body.password,
                                  caps=None, roles=s.registration_default_roles)
    except ValidationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_IDENTIFIER")
    except UserExists:
        raise HTTPException(status.HTTP_409_CONFLICT, "USERNAME_TAKEN")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    if s.EMAIL_VERIFICATION_ENABLED:
        # Sent, and the OUTCOME recorded. A fire-and-forget send means a mail provider outage
        # produces a stream of accounts that can never log in, with nothing in the response or
        # the logs to say why. The account still exists either way — failing registration because
        # a mail queue hiccuped would be the worse trade — but "we could not send it" is a fact
        # somebody needs.
        if not await _send_verification(request, svc, user):
            get_logger("app.auth").warning(
                "verification_mail_not_sent",
                extra={"tenant": user.tenant, "detail": "account created but no mail accepted; "
                                                        "the user must use resend-verification"})
    await request.app.state.auditor.record(
        "auth.registered", actor=user.id, tenant=user.tenant,
        resource=f"user:{user.id}", ip=request.client.host if request.client else "")
    return user.public()


async def _send_verification(request: Request, svc: AuthService, user) -> bool:
    """Mint a token and hand it to the notifier. Returns whether the mail was accepted.

    The raw token goes to the NOTIFIER and never to the HTTP response — a verification token in a
    response body would let anyone who can call register prove an address they do not own, which
    is the entire property this feature exists to establish.
    """
    s = get_settings()
    token = await svc.start_verification(user)
    if token is None:
        return False
    return await request.app.state.container.notifier.send(
        to=user.username, template=s.EMAIL_VERIFICATION_TEMPLATE, tenant=user.tenant,
        variables={"verify_url": f"{s.EMAIL_VERIFICATION_URL}{token}",
                   "address": user.username,
                   # A stable reference for the notifier's idempotency key. It is a HASH of the
                   # token, never the token: the key travels through logs and dedupe stores.
                   "token_ref": hashlib.sha256(token.encode()).hexdigest()[:32]})


@router.post("/verify-email", dependencies=[Depends(rate_limit)])
async def verify_email(request: Request, body: VerifyEmailIn) -> dict:
    """Redeem a verification token. Single-use.

    Unknown, expired and already-spent are ONE answer on purpose. Distinguishing them tells an
    attacker holding a stale link whether the address exists and whether someone got there first,
    and none of those distinctions helps a legitimate user — who needs the same next step in
    every case: ask for another mail.
    """
    user = await _svc(request).verify_email(body.token)
    if user is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_OR_EXPIRED_TOKEN")
    await request.app.state.auditor.record(
        "auth.email_verified", actor=user.id, tenant=user.tenant,
        resource=f"user:{user.id}", ip=request.client.host if request.client else "")
    return {"verified": True}


@router.post("/resend-verification", status_code=status.HTTP_202_ACCEPTED,
             dependencies=[Depends(rate_limit)])
async def resend_verification(request: Request, body: ResendIn) -> dict:
    """Ask for another verification mail. ALWAYS 202, whatever the truth.

    This route is unauthenticated, so every distinguishable outcome is an oracle: whether the
    address is registered, whether it is already verified, whether it is in cooldown. All of them
    answer the same way. A caller who owns the address learns the truth from their inbox, which is
    the only place that should be telling them.
    """
    s = get_settings()
    svc = _svc(request)
    user = await UserRepository(request.app.state.container.datastore,
                               request.app.state.container.login_identifier
                               ).get_by_username(body.tenant, body.username)
    if user is not None and not svc.email_is_proven(user) and svc._verifications is not None:
        if not await svc._verifications.recently_issued(
                user.id, s.EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS):
            await _send_verification(request, svc, user)
    return {"status": "accepted"}


@router.post("/login", dependencies=[Depends(rate_limit)])
async def login(request: Request, body: LoginIn) -> dict:
    svc = _svc(request)
    ip = request.client.host if request.client else ""

    async def _failed(reason: str) -> None:
        # A FAILED login is recorded in full: it is the primary signal of brute-force and
        # credential-stuffing, and ISO 27001 (A.8.15), NIST SP 800-53 (AU-2) and PCI-DSS (10.2)
        # all require it. No principal exists, so — per the same standards' partial-identifier
        # rule — the ATTEMPTED identifier and the source IP ARE the actor.
        await request.app.state.auditor.record(
            "auth.login_failed", actor=body.username, tenant=body.tenant,
            resource=f"auth:login:{body.tenant}", ip=ip, metadata={"reason": reason})

    user = await svc.authenticate(body.tenant, body.username, body.password)
    if user is None:
        await _failed("invalid_credentials")
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
    # MFA gate: if the user has enrolled TOTP and MFA is active, require a code.
    c = request.app.state.container
    if c.mfa.enabled and user.mfa_secret:
        if not body.code or not c.mfa.verify(user.mfa_secret, body.code):
            await _failed("mfa_required")
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA_REQUIRED")
        await request.app.state.auditor.record(
            "auth.mfa_verified", actor=user.id, tenant=user.tenant,
            resource=f"user:{user.id}", ip=ip)
    try:
        tokens = await svc.issue_tokens(user)
    except AuthService.EmailNotVerified:
        # 403, NOT 401. The credentials were correct — telling this user their password is wrong
        # sends them into a reset loop that cannot fix anything. It does reveal that the account
        # exists and is unverified, but only to someone who ALREADY proved they know the
        # password, so it is not a free enumeration oracle.
        await _failed("email_not_verified")
        raise HTTPException(status.HTTP_403_FORBIDDEN, "EMAIL_NOT_VERIFIED")
    await request.app.state.auditor.record(
        "auth.login_succeeded", actor=user.id, tenant=user.tenant,
        resource=f"user:{user.id}", ip=ip)
    return tokens


def _session_of(request: Request, token: str) -> tuple[str, str]:
    """Best-available actor for a token-only route: the (subject, jti) the refresh token still
    decodes to. The token is validated for signature and expiry — enough to attribute a renewal or
    a logout without a bound principal. Returns ("","") if it cannot be read."""
    try:
        claims = request.app.state.container.token_verifier.verify(token)
        return str(claims.get("sub", "")), str(claims.get("jti", ""))
    except Exception:  # noqa: BLE001 — attribution is best-effort; the action still records
        return "", ""


@router.post("/refresh")
async def refresh(request: Request, body: RefreshIn) -> dict:
    try:
        tokens = await _svc(request).refresh(body.refresh_token)
    except CapsUnavailable:
        # Refresh MINTS, so it resolves capabilities too — and it must fail the same way. A 401
        # here would be a lie: the session is fine, the capability authority is down. A client that
        # believes 401 logs the user out and sends them to a login page that will also 503.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "CAPS_UNAVAILABLE")
    if tokens is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH")
    sub, jti = _session_of(request, body.refresh_token)
    await request.app.state.auditor.record(
        "auth.token_refreshed", actor=sub, resource=f"session:{jti}" if jti else "session",
        ip=request.client.host if request.client else "")
    return tokens


@router.post("/logout")
async def logout(request: Request, body: RefreshIn) -> dict:
    sub, jti = _session_of(request, body.refresh_token)
    await _svc(request).logout(body.refresh_token)
    await request.app.state.auditor.record(
        "auth.logged_out", actor=sub, resource=f"session:{jti}" if jti else "session",
        ip=request.client.host if request.client else "")
    return {"status": "ok"}


@router.get("/me")
async def me(request: Request, principal: Principal = Depends(require_user),
             guard=Depends(get_guard)) -> dict:
    user = await UserRepository(request.app.state.container.datastore, request.app.state.container.login_identifier).get(principal.subject)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    async with guard.step("respond") as step:
        body = user.public()  # excludes password_hash / mfa_secret
        step.result = body
    return body


@router.delete("/me", dependencies=[Depends(rate_limit)])
async def delete_me(request: Request,
                    principal: Principal = Depends(require_any_capability("identity.self"))) -> dict:
    """A person erases themselves: revoke this membership; delete the Person on the last one.

    The subject is the caller's OWN token — there is no id to pass, so this cannot be aimed at
    anyone else. Refused for the tenant's sole owner (that would strand the tenant — close it
    instead). Idempotent when the membership is already gone.
    """
    # Write-ahead: a critical action the log cannot take must not happen at all.
    await request.app.state.auditor.record(
        "auth.self_deleted", actor=principal.subject, tenant=principal.tenant,
        resource=f"user:{principal.subject}",
        ip=request.client.host if request.client else "")
    try:
        res = await _svc(request).delete_self(principal.subject, principal.tenant)
    except SoleOwnerError:
        raise HTTPException(status.HTTP_409_CONFLICT, "SOLE_OWNER")
    if not res.get("deleted") and res.get("reason") == "NOT_FOUND":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    return res


@router.post("/change-password")
async def change_password(request: Request, body: ChangePwIn,
                          principal: Principal = Depends(require_user)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "auth.password_changed", actor=principal.subject, tenant=principal.tenant,
        resource=f"user:{principal.subject}",
        ip=request.client.host if request.client else "")
    repo = UserRepository(request.app.state.container.datastore, request.app.state.container.login_identifier)
    user = await repo.get(principal.subject)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    try:
        ok = await _svc(request).change_password(user, body.old_password, body.new_password)
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    if not ok:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_PASSWORD")
    return {"status": "ok"}


@router.post("/reset-password", dependencies=[Depends(rate_limit)])
async def reset_password(request: Request, body: ResetPwIn) -> dict:
    """Set a new password from a verified reset-OTP — the account-recovery path, NO old password.

    The OTP (purpose=reset) is the authorisation: proving control of the phone/email is what licenses a
    reset without the prior secret. The code is verified — and CONSUMED — here, atomically with the
    password write, so there is no separately-callable "prove it to the browser, record nothing" step
    (the incident that keeps /otp/verify off the gateway); callers post {identifier, code, new_password}
    in one request and must NOT pre-call /otp/verify (that would spend the single-use code).

    Errors leak NO oracle: a wrong code and an unknown/inactive identifier both collapse to
    INVALID_RESET. WEAK_PASSWORD is distinct AND checked BEFORE the code is consumed, so a weak password
    never burns a locked-out admin's only recovery code."""
    from starlette.responses import JSONResponse

    from app.core.otp_service import OtpError
    from app.middleware.request_context import get_request_id

    def _err(code: str, status_code: int = status.HTTP_400_BAD_REQUEST) -> JSONResponse:
        # A NAMED code (not the status→BAD_REQUEST generic), returned directly so the console can tell a
        # weak password from a bad code. Justified like CapsNotGrantable: neither name leaks — WEAK_
        # PASSWORD is about the caller's OWN input, INVALID_RESET is a single bucket that never
        # distinguishes a wrong code from an unknown identifier. Goes through the header middleware.
        payload: dict = {"error": code}
        rid = get_request_id()
        if rid:
            payload["request_id"] = rid
        return JSONResponse(payload, status_code=status_code)

    svc = _svc(request)
    # 1) strength FIRST — before the OTP is spent (so a typo'd weak password is retryable).
    try:
        svc.ensure_password_strength(body.new_password)
    except WeakPassword:
        return _err("WEAK_PASSWORD")
    # 2) verify + consume the reset code (same canonical key the OTP was issued under).
    from app.api.v1.otp import _canonical
    otp = request.app.state.container.otp_service
    try:
        await otp.verify(identifier=_canonical(request, body.identifier),
                         code=body.code, purpose="reset")
    except OtpError:
        return _err("INVALID_RESET")
    # 3) resolve the account — a valid code for an unknown/inactive identifier gives the SAME error.
    person = await svc.resolve_for_reset(body.identifier)
    if person is None:
        return _err("INVALID_RESET")
    # 4) write-ahead audit (the id, never the raw identifier — no PII in the log), then set.
    await request.app.state.auditor.record(
        "auth.password_reset", actor=person.id, tenant="",
        resource=f"user:{person.id}", ip=request.client.host if request.client else "")
    await svc.set_password(person, body.new_password)
    return {"status": "ok"}


@router.post("/mfa/enroll")
async def mfa_enroll(request: Request, principal: Principal = Depends(require_user)) -> dict:
    # Write-ahead: a critical action the log cannot take must not happen at all, so this
    # runs BEFORE the mutation. Recording afterwards would 503 a caller whose action had
    # already succeeded, and leave it unrecorded.
    await request.app.state.auditor.record(
        "auth.mfa_enrolled", actor=principal.subject, tenant=principal.tenant,
        resource=f"user:{principal.subject}",
        ip=request.client.host if request.client else "")
    c = request.app.state.container
    if not c.mfa.enabled:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA_DISABLED")
    repo = UserRepository(c.datastore, c.login_identifier)
    user = await repo.get(principal.subject)
    if user is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "NOT_FOUND")
    provisioned = c.mfa.provision(user.username)
    user.mfa_secret = provisioned["secret"]
    await repo.update(user)
    return {"uri": provisioned["uri"], "secret": provisioned["secret"]}


@router.post("/mfa/verify")
async def mfa_verify(request: Request, body: MfaVerifyIn,
                     principal: Principal = Depends(require_user)) -> dict:
    c = request.app.state.container
    repo = UserRepository(c.datastore, c.login_identifier)
    user = await repo.get(principal.subject)
    if user is None or not user.mfa_secret:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "MFA_NOT_ENROLLED")
    if not c.mfa.verify(user.mfa_secret, body.code):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CODE")
    return {"verified": True}
