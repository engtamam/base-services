# =============================================================================
# identity-service — auth routes (/api/v1/auth)
# -----------------------------------------------------------------------------
# register · login · refresh · logout · me · change-password · MFA enroll/verify
# Token-returning routes intentionally emit tokens; the /me profile is wrapped in
# the CWE respond-guard to guarantee it never leaks password_hash/mfa_secret.
# =============================================================================
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.auth import Principal, require_user
from app.api.deps import get_guard
from app.api.ratelimit import rate_limit
from app.config import get_settings
from app.core.auth_service import AuthService, WeakPassword
from app.ports.errors import ValidationError
from app.core.login_attempts import LoginAttemptStore
from app.core.membership import MembershipStore
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserExists, UserRepository

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Schemas ─────────────────────────────────────────────────────────────────
class RegisterIn(BaseModel):
    username: str = Field(min_length=3, max_length=128)
    # min length is enforced by the core policy (PASSWORD_MIN_LENGTH); max is a DoS cap.
    password: str = Field(min_length=1, max_length=256)
    tenant: str = Field(default="default", max_length=128)
    caps: list[str] = Field(default_factory=list)
    roles: list[str] = Field(default_factory=list)


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
    )


# ── Routes ──────────────────────────────────────────────────────────────────
@router.post("/register", status_code=status.HTTP_201_CREATED, dependencies=[Depends(rate_limit)])
async def register(request: Request, body: RegisterIn) -> dict:
    if not get_settings().REGISTRATION_ENABLED:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "REGISTRATION_DISABLED")
    svc = _svc(request)
    try:
        user = await svc.register(body.tenant, body.username, body.password, body.caps, body.roles)
    except ValidationError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "INVALID_IDENTIFIER")
    except UserExists:
        raise HTTPException(status.HTTP_409_CONFLICT, "USERNAME_TAKEN")
    except WeakPassword:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "WEAK_PASSWORD")
    return user.public()


@router.post("/login", dependencies=[Depends(rate_limit)])
async def login(request: Request, body: LoginIn) -> dict:
    svc = _svc(request)
    user = await svc.authenticate(body.tenant, body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_CREDENTIALS")
    # MFA gate: if the user has enrolled TOTP and MFA is active, require a code.
    c = request.app.state.container
    if c.mfa.enabled and user.mfa_secret:
        if not body.code or not c.mfa.verify(user.mfa_secret, body.code):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "MFA_REQUIRED")
    return await svc.issue_tokens(user)


@router.post("/refresh")
async def refresh(request: Request, body: RefreshIn) -> dict:
    tokens = await _svc(request).refresh(body.refresh_token)
    if tokens is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "INVALID_REFRESH")
    return tokens


@router.post("/logout")
async def logout(request: Request, body: RefreshIn) -> dict:
    await _svc(request).logout(body.refresh_token)
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


@router.post("/change-password")
async def change_password(request: Request, body: ChangePwIn,
                          principal: Principal = Depends(require_user)) -> dict:
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


@router.post("/mfa/enroll")
async def mfa_enroll(request: Request, principal: Principal = Depends(require_user)) -> dict:
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
