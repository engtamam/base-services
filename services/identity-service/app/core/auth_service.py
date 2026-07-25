"""Core authentication logic — framework-free, depends only on ports.

Security properties:
  * passwords hashed (argon2/bcrypt) — never stored/returned in plaintext
  * login is timing-safe & does NOT reveal whether a username exists (CWE-204)
  * refresh tokens ROTATE and are REVOCABLE — a replayed old refresh is rejected
"""
from __future__ import annotations

import datetime as _dt

from app.core.membership import (
    ACTIVE,
    PENDING,
    UNUSABLE_PASSWORD,
    Membership,
    Person,
)
from app.core.models import User, new_id
from app.core.refresh_store import RefreshStore
from app.core.tenancy import TenantAccessDenied, authorize_tenant, claims_for
from app.core.user_repository import UserRepository
from app.ports.errors import InvalidToken

DEFAULT_MIN_PASSWORD_LEN = 8  # fallback default; real value comes from config

# How a person who does not exist yet gets a password. The PLATFORM decides; we enforce.
INVITE, PASSWORD = "invite", "password"
ADD_MODES = (INVITE, PASSWORD)


class WeakPassword(Exception):
    pass


class CredentialNotYours(Exception):
    """A tenant admin tried to set the credential of a person who already exists (T2)."""


class AuthService:
    # Class-level defaults so a partially-constructed instance (tests build these directly) has
    # the SAFE value rather than an AttributeError — and so that adding a dependency here can
    # never silently turn a guard off in production, where __init__ always runs.
    _verifications = None
    _require_verified = False
    _legacy_email = "grandfather"
    _platform_tenant = ""

    def __init__(self, *, users: UserRepository, refresh: RefreshStore, hasher,
                 issuer, verifier, mfa, identifier, access_ttl: int, refresh_ttl: int,
                 min_password_len: int = DEFAULT_MIN_PASSWORD_LEN, lockout=None,
                 memberships=None, invites=None, caps_resolver=None,
                 platform_tenant: str = "", verifications=None,
                 require_verified_email: bool = False,
                 legacy_email_policy: str = "grandfather") -> None:
        self._platform_tenant = platform_tenant
        self._verifications = verifications
        self._require_verified = require_verified_email
        self._legacy_email = legacy_email_policy
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
        self._invites = invites            # InviteStore | None (invite mode)
        self._caps = caps_resolver         # CapsResolver | None (None => membership caps)
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

    def email_is_proven(self, user: User) -> bool:
        """Has this account proven its address, under the configured legacy policy?

        THE THREE-VALUED CHECK. `None` means the row predates verification and nobody ever asked;
        `False` means we asked and they have not answered. `grandfather` accepts the first and
        refuses the second, which is what lets this ship without locking out every existing user
        on the day it deploys. `enforce` refuses both and is the honest setting for a platform
        that wants the guarantee to hold retroactively.

        Written as an explicit `is True` / `is None` pair rather than a truthiness test, because
        `not user.email_verified` would silently merge the two cases this method exists to keep
        apart.
        """
        if user.email_verified is True:
            return True
        if user.email_verified is None:
            return self._legacy_email != "enforce"
        return False

    class EmailNotVerified(Exception):
        """Correct credentials, unproven address. Deliberately NOT an authentication failure."""

    def _safe_caps(self, user: User) -> list[str]:
        """Strip platform-wide capabilities from a token that is not the platform tenant's.

        THE LAST DOOR. `platform.*` was a naming convention and never a boundary — nothing in
        require_capability or at the gateway refused a platform.* cap on a customer's token. So a
        capability that ended up on a User by ANY route became platform authority on login.

        `*` is stripped too, and it is the one that mattered most: the gateway honours it as a
        wildcard matching every capability check on the platform, so `*` was strictly worse than
        platform.admin and easy to miss while looking for the word "platform".

        Filtered at MINT time rather than at each check, for the reason tenancy.py already gives:
        the moment a token is minted is the only moment anything is verified. Everywhere else the
        claim is simply believed, and sixteen services are right to believe it.

        PLATFORM_TENANT empty disables this — the pre-existing behaviour, kept so an upgrade
        cannot lock an operator out of their own platform, and logged at boot so it is a decision
        rather than a silence.
        """
        caps = list(user.caps or [])
        platform_tenant = (self._platform_tenant or "").strip()
        if not platform_tenant or user.tenant == platform_tenant:
            return caps
        return [c for c in caps if c != "*" and not c.startswith("platform.")]

    # ── email verification ───────────────────────────────────────────────────
    async def start_verification(self, user: User) -> str | None:
        """Mint a token and ask for it to be mailed. Returns the raw token, or None if disabled.

        The token is returned to the CALLER so the route can decide what to do with it — the
        route hands it to the notifier and never to the HTTP response. Returning it here rather
        than sending from inside keeps this service testable without a mail provider, and keeps
        the "who is allowed to see this" decision at one visible boundary.
        """
        if self._verifications is None:
            return None
        return await self._verifications.issue(
            user_id=user.id, tenant=user.tenant, address=user.username)

    async def verify_email(self, token: str) -> User | None:
        """Redeem a token and mark the address proven. None if the token is not good.

        The address on the TOKEN must still match the account's address. Without that check a
        link minted for the old address would prove a NEW one after a change of email — proving
        something nobody ever demonstrated control of, which is the whole property this feature
        sells.
        """
        if self._verifications is None:
            return None
        row = await self._verifications.redeem(token)
        if row is None:
            return None
        user = await self._users.get(str(row.get("user_id", "")))
        if user is None:
            return None
        if self._identifier.display(user.username) != self._identifier.display(
                str(row.get("address", ""))):
            return None
        if user.email_verified is True:
            return user                     # already proven; redeeming again is harmless
        user.email_verified = True
        await self._users.save(user)
        return user

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
        # THE GATE, at the mint rather than in the login route. tenancy.py already argues why:
        # minting is the only moment anything is verified, and every service downstream simply
        # believes the claim. A check in one handler is one new handler away from being bypassed —
        # and refresh mints too, so a route-level check would let an unverified session live for
        # ever on renewals.
        if self._require_verified and not self.email_is_proven(user):
            raise self.EmailNotVerified(user.username)
        claims = {"caps": self._safe_caps(user), "roles": user.roles, "tenant": user.tenant}
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
        if person is None or person.password_hash == UNUSABLE_PASSWORD:
            # An INVITED person who has not claimed their account has no password that works, and
            # they must look exactly like a person who does not exist: same timing, same answer.
            # Otherwise login becomes an oracle for "this identifier exists and is unclaimed" —
            # which is precisely the account worth attacking, since nobody is watching it yet.
            # The sentinel is checked BEFORE the hasher: a hasher handed a non-hash may raise, and
            # a 500 here would be the loudest oracle of all.
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

    # ── the directory's write side: an admin staffs a tenant ──────────────────
    async def add_member(self, tenant: str, identifier: str, *, roles=None, caps=None,
                         password: str | None = None, mode: str = INVITE,
                         invite_ttl: int = 0) -> tuple[Membership, str | None]:
        """Add someone to THIS tenant. Returns the membership and, in invite mode, a one-time token.

        THE HARD BOUNDARY, and it holds in every mode:

            If the person ALREADY EXISTS, we grant a membership and touch NOTHING ELSE.

        Not their password, not their MFA, not their identifier, not their status. The freelancer
        being added to your company already works for two others; their credential is the key to
        those companies too, and an admin here has no claim on it (T2, CWE-640). Adding someone to
        your company is a statement about YOUR company, not about them.

        For a person who does not exist yet, the platform chooses:

          invite   (default) the person is created with an UNUSABLE password and a one-time token
                   is returned once. The human sets their own password; the admin never knows it.
          password the admin sets an initial password. Simpler, and it lets an admin pre-register
                   an identifier they do not control — see invites.py. Opt-in, eyes open.
        """
        if self._memberships is None:
            raise RuntimeError("IDENTITY_MULTI_TENANT is off — no membership store is wired")
        if not tenant:
            raise TenantAccessDenied()

        self._identifier.validate(identifier)
        ident = self._identifier.display(identifier)
        person = await self._memberships.person_by_identifier(ident)
        invite: str | None = None

        if person is None:
            if mode == PASSWORD:
                if not password or len(password) < self._min_pw:
                    raise WeakPassword(f"password must be >= {self._min_pw} characters")
                pw_hash, status = self._hasher.hash(password), ACTIVE
            else:
                # The admin creates the account and cannot log into it. That is the point.
                pw_hash, status = UNUSABLE_PASSWORD, PENDING
            person = await self._memberships.create_person(Person(
                id=new_id(), identifier=ident, password_hash=pw_hash, status=status))
            if mode == INVITE:
                invite = await self._invites.issue(person.id, tenant, invite_ttl)
        elif password is not None:
            # An admin supplying a password for a person who already exists is either confused or
            # attacking. There is no reading of it that is safe, so it is refused rather than
            # quietly ignored — a silent ignore teaches them it worked.
            raise CredentialNotYours(
                "this person already exists; a tenant admin may grant them a membership but may "
                "never set their password — it unlocks every other company they work for"
            )

        existing = await self._memberships.membership(person.id, tenant)
        if existing is not None:
            return existing, None            # idempotent: adding twice is not an escalation

        m = await self._memberships.grant(Membership(
            person_id=person.id, tenant=tenant,
            caps=list(caps or []), roles=list(roles or []),
            # A membership for a person who has not claimed their account yet is PENDING: it shows
            # in the directory (the manager can see who they invited) but mints no token, because
            # `Membership.active` is what the mint checks.
            status=PENDING if person.status == PENDING else ACTIVE,
        ))
        return m, invite

    async def remove_member(self, tenant: str, person_id: str) -> bool:
        """Revoke ONE membership. The person survives — she still works somewhere else.

        Deleting the Person here would delete her access to every other company on the platform,
        from one company's admin panel. Revoking is a statement about this tenant and no other.
        Refresh re-verifies membership (T4), so this bites within the access-token TTL.
        """
        if self._memberships is None:
            raise RuntimeError("IDENTITY_MULTI_TENANT is off — no membership store is wired")
        if not tenant or not person_id:
            raise TenantAccessDenied()
        if await self._memberships.membership(person_id, tenant) is None:
            return False
        await self._memberships.revoke(person_id, tenant)
        return True

    async def update_member_roles(self, tenant: str, person_id: str, *,
                                  roles=None, caps=None):
        """Change an EXISTING member's roles/caps in this tenant — never their credential (T2).

        Without this, changing what a member may do meant removing and re-inviting them, which drops
        the membership and forces a fresh credential claim. This touches only the roles/caps on the
        membership row, in this one tenant. Returns the updated membership, or None if absent."""
        if self._memberships is None:
            raise RuntimeError("IDENTITY_MULTI_TENANT is off — no membership store is wired")
        if not tenant or not person_id:
            raise TenantAccessDenied()
        m = await self._memberships.membership(person_id, tenant)
        if m is None:
            return None
        if roles is not None:
            m.roles = list(roles)
        if caps is not None:
            m.caps = list(caps)
        return await self._memberships.grant(m)

    async def claim_invite(self, token: str, password: str) -> Person | None:
        """The invited human sets their OWN password. Single-use; the admin never sees it."""
        if self._memberships is None or self._invites is None:
            raise RuntimeError("IDENTITY_MULTI_TENANT is off — no membership store is wired")
        if len(password) < self._min_pw:
            raise WeakPassword(f"password must be >= {self._min_pw} characters")

        row = await self._invites.redeem(token)      # consumed here: an invite works exactly once
        if row is None:
            return None
        person = await self._memberships.person(str(row.get("person_id", "")))
        if person is None or person.status != PENDING:
            # Only a PENDING person may be claimed. Otherwise a stale invite for someone who has
            # since set a password would be a password reset for whoever kept the link.
            return None

        person.password_hash = self._hasher.hash(password)
        person.status = ACTIVE
        await self._memberships.save_person(person)

        m = await self._memberships.membership(person.id, str(row.get("tenant", "")))
        if m is not None and m.status == PENDING:
            m.status = ACTIVE
            await self._memberships.grant(m)
        return person

    async def issue_tenant_tokens(self, person: Person, membership: Membership) -> dict:
        """Mint a token for EXACTLY ONE tenant — the one on the membership row we verified.

        Note there is no `tenant` parameter. The tenant is read off the membership, never off the
        request (T1), so a caller cannot smuggle one past the check even by mistake.
        """
        # Roles are the INPUT; capabilities are DERIVED. Resolved for THIS tenant and no other, so
        # an admin at her own company is still a read-only contractor at the bank (T3).
        #
        # If the authority cannot be reached this RAISES and no token is minted. That is
        # deliberate: minting a token with no capabilities would look exactly like a permissions
        # bug, and the way a permissions bug gets "fixed" is by granting capabilities by hand — so
        # a brief authz outage would quietly become a permanent over-grant nobody revokes.
        caps = None
        if self._caps is not None:
            caps = await self._caps.resolve(
                subject=person.id, tenant=membership.tenant,
                roles=list(membership.roles), caps=list(membership.caps))

        claims = claims_for(person, membership, caps)    # raises TenantAccessDenied if not current
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
