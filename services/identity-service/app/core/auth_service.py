"""Core authentication logic — framework-free, depends only on ports.

Security properties:
  * passwords hashed (argon2/bcrypt) — never stored/returned in plaintext
  * login is timing-safe & does NOT reveal whether a username exists (CWE-204)
  * refresh tokens ROTATE and are REVOCABLE — a replayed old refresh is rejected
"""
from __future__ import annotations

import datetime as _dt

from app.core.models import User, new_id
from app.core.refresh_store import RefreshStore
from app.core.user_repository import UserRepository
from app.ports.errors import InvalidToken

DEFAULT_MIN_PASSWORD_LEN = 8  # fallback default; real value comes from config


class WeakPassword(Exception):
    pass


class AuthService:
    def __init__(self, *, users: UserRepository, refresh: RefreshStore, hasher,
                 issuer, verifier, mfa, identifier, access_ttl: int, refresh_ttl: int,
                 min_password_len: int = DEFAULT_MIN_PASSWORD_LEN, lockout=None) -> None:
        self._users = users
        self._identifier = identifier
        self._refresh = refresh
        self._hasher = hasher
        self._issuer = issuer
        self._verifier = verifier
        self._mfa = mfa
        self._access_ttl = access_ttl
        self._refresh_ttl = refresh_ttl
        self._min_pw = min_password_len
        self._lockout = lockout            # LoginAttemptStore | None (per-account brute-force)
        # Precomputed dummy hash so a missing user costs the same as a real one.
        self._dummy_hash = hasher.hash("timing-equalizer")

    # ── registration ────────────────────────────────────────────────────────
    async def register(self, tenant: str, username: str, password: str,
                       caps: list[str] | None = None, roles: list[str] | None = None) -> User:
        self._identifier.validate(username)          # a malformed email is a 400, not a stored row
        if len(password) < self._min_pw:
            raise WeakPassword(f"password must be >= {self._min_pw} characters")
        user = User(
            id=new_id(), tenant=tenant, username=self._identifier.display(username),
            password_hash=self._hasher.hash(password),
            caps=list(caps or []), roles=list(roles or []),
        )
        return await self._users.create(user)

    # ── login ────────────────────────────────────────────────────────────────
    async def authenticate(self, tenant: str, username: str, password: str) -> User | None:
        # Locked accounts are refused before any password work (and without revealing
        # whether the account exists — the caller returns the same generic failure).
        if self._lockout and await self._lockout.is_locked(tenant, username):
            return None
        user = await self._users.get_by_username(tenant, username)
        if user is None:
            self._hasher.verify(password, self._dummy_hash)   # equalize timing
            if self._lockout:
                await self._lockout.record_failure(tenant, username)
            return None
        if not self._hasher.verify(password, user.password_hash):
            if self._lockout:
                await self._lockout.record_failure(tenant, username)
            return None
        if user.status != "active":
            return None
        if self._lockout:
            await self._lockout.reset(tenant, username)       # a good login clears the counter
        return user

    # ── token issuance ────────────────────────────────────────────────────────
    async def issue_tokens(self, user: User) -> dict:
        claims = {"caps": user.caps, "roles": user.roles, "tenant": user.tenant}
        access = self._issuer.issue_access(user.id, claims, self._access_ttl)
        jti = new_id()
        refresh = self._issuer.issue_refresh(user.id, jti, self._refresh_ttl)
        exp_ts = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(seconds=self._refresh_ttl)).timestamp()
        await self._refresh.issue(jti, user.id, exp_ts)
        return {
            "token_type": "Bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self._access_ttl,
        }

    # ── refresh (rotation) ─────────────────────────────────────────────────────
    async def refresh(self, refresh_token: str) -> dict | None:
        try:
            claims = self._verifier.verify(refresh_token)
        except InvalidToken:
            return None
        if claims.get("type") != "refresh":
            return None
        jti = claims.get("jti")
        if not jti:
            return None
        state = await self._refresh.status(jti)
        if state == "revoked":
            # A REUSED (already-rotated) refresh token → theft signal. Revoke the whole
            # family so a stolen token can't keep minting sessions.
            await self._refresh.revoke_family(str(claims.get("sub", "")))
            return None
        if state != "active":
            return None
        await self._refresh.revoke(jti)  # rotate: old refresh can never be reused
        user = await self._users.get(str(claims.get("sub", "")))
        if user is None or user.status != "active":
            return None
        return await self.issue_tokens(user)

    # ── logout ─────────────────────────────────────────────────────────────────
    async def logout(self, refresh_token: str) -> bool:
        try:
            claims = self._verifier.verify(refresh_token)
        except InvalidToken:
            return False
        jti = claims.get("jti")
        if jti:
            await self._refresh.revoke(jti)
        return True

    # ── change password ────────────────────────────────────────────────────────
    async def change_password(self, user: User, old_password: str, new_password: str) -> bool:
        if not self._hasher.verify(old_password, user.password_hash):
            return False
        if len(new_password) < self._min_pw:
            raise WeakPassword(f"password must be >= {self._min_pw} characters")
        user.password_hash = self._hasher.hash(new_password)
        await self._users.update(user)
        return True
