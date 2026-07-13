"""Core authentication logic — framework-free, depends only on ports.

Security properties:
  * passwords hashed (argon2/bcrypt) — never stored/returned in plaintext
  * login is timing-safe & does NOT reveal whether a username exists (CWE-204)
  * refresh tokens ROTATE and are REVOCABLE — a replayed old refresh is rejected
"""
from __future__ import annotations

import datetime as _dt

from app.core.membership import ACTIVE, Membership, Person
from app.core.models import User, new_id
from app.core.refresh_store import RefreshStore
from app.core.tenancy import TenantAccessDenied, authorize_tenant, claims_for
from app.core.user_repository import UserRepository
from app.ports.errors import InvalidToken

DEFAULT_MIN_PASSWORD_LEN = 8  # fallback default; real value comes from config


class WeakPassword(Exception):
    pass


class AuthService:
    def __init__(self, *, users: UserRepository, refresh: RefreshStore, hasher,
                 issuer, verifier, mfa, identifier, access_ttl: int, refresh_ttl: int,
                 min_password_len: int = DEFAULT_MIN_PASSWORD_LEN, lockout=None,
                 memberships=None) -> None:
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
        # MembershipStore | None. None => IDENTITY_MULTI_TENANT is off and every path below
        # behaves exactly as it did before Phase 31. Multi-tenancy is opt-in, not a migration.
        self._memberships = memberships
        # Precomputed dummy hash so a missing user costs the same as a real one.
        self._dummy_hash = hasher.hash("timing-equalizer")

    @property
    def memberships(self):
        """The MembershipStore, or None when IDENTITY_MULTI_TENANT is off."""
        return self._memberships

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

    # ── multi-tenant: authenticate the PERSON, then mint for ONE tenant ────────
    async def authenticate_person(self, identifier: str, password: str,
                                  source: str = "") -> Person | None:
        """Authenticate the global person. Mints NOTHING — no tenant is chosen yet.

        Lockout is keyed on `(person, source)`, not on the person alone. Login is now against a
        GLOBAL identity, so a counter keyed on the person alone hands an unauthenticated attacker
        a way to spam bad passwords for `consultant@x.com` and lock them out of all five of their
        clients at once (T5). We would have turned a per-tenant nuisance into a way to remove a
        consultant from their entire livelihood. Failing into a total denial of service is not
        "secure by default"; it is just a different outage.
        """
        if self._memberships is None:
            raise RuntimeError("IDENTITY_MULTI_TENANT is off — no membership store is wired")
        ident = self._identifier.display(identifier)
        if self._lockout and await self._lockout.is_locked(source, ident):
            return None
        person = await self._memberships.person_by_identifier(ident)
        if person is None:
            self._hasher.verify(password, self._dummy_hash)   # equalize timing (CWE-204)
            if self._lockout:
                await self._lockout.record_failure(source, ident)
            return None
        if not self._hasher.verify(password, person.password_hash):
            if self._lockout:
                await self._lockout.record_failure(source, ident)
            return None
        if person.status != ACTIVE:
            return None
        if self._lockout:
            await self._lockout.reset(source, ident)
        return person

    async def issue_tenant_tokens(self, person: Person, membership: Membership) -> dict:
        """Mint a token for EXACTLY ONE tenant — the one on the membership row we verified.

        Note there is no `tenant` parameter. The tenant is read off the membership, never off the
        request (T1), so a caller cannot smuggle one past the check even by mistake.
        """
        claims = claims_for(person, membership)          # raises TenantAccessDenied if not current
        access = self._issuer.issue_access(person.id, claims, self._access_ttl)
        jti = new_id()
        refresh = self._issuer.issue_refresh(person.id, jti, self._refresh_ttl)
        exp_ts = (_dt.datetime.now(_dt.timezone.utc)
                  + _dt.timedelta(seconds=self._refresh_ttl)).timestamp()
        await self._refresh.issue(jti, person.id, exp_ts, tenant=membership.tenant)
        return {
            "token_type": "Bearer",
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": self._access_ttl,
        }

    async def select_tenant(self, person: Person, tenant: str) -> dict:
        """Login → choose a door. Raises TenantAccessDenied — uniformly, saying nothing (T6)."""
        membership = await authorize_tenant(self._memberships, person, tenant)
        return await self.issue_tenant_tokens(person, membership)

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

        # Read the tenant BEFORE rotating — revoke() rewrites the row we are reading.
        tenant = await self._refresh.tenant_of(jti)
        await self._refresh.revoke(jti)  # rotate: old refresh can never be reused
        sub = str(claims.get("sub", ""))

        if tenant:
            # THE MOST DANGEROUS LINE IN THIS PHASE, and it is now a check.
            #
            # Fire a contractor and their access token stays valid for its remaining TTL — that is
            # bounded and acceptable. Their REFRESH token must not quietly hand them a new one for
            # the whole refresh lifetime (T4, CWE-613). So the membership is re-verified on every
            # refresh, and revocation bites here rather than at TTL. A suspended person, a
            # suspended membership, and a membership that simply no longer exists all fail.
            if self._memberships is None:
                return None      # a tenant-scoped refresh with no store to check it: fail CLOSED
            person = await self._memberships.person(sub)
            if person is None:
                return None
            try:
                membership = await authorize_tenant(self._memberships, person, tenant)
            except TenantAccessDenied:
                return None
            return await self.issue_tenant_tokens(person, membership)

        user = await self._users.get(sub)
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
