"""Machine identity — a tenant-scoped service account and the OAuth2 client-credentials grant.

Framework-free; depends only on a datastore and a password hasher. This is the inbound mirror of
connector-service's outbound OAuth2: a partner's own code (a POS, an ERP) that calls our API with no
browser, no inbox, and no person behind it gets a credential OF ITS OWN — not a borrowed human
password, not a human's refresh token, not the platform-wide internal token.

Security properties, and the standard each answers to:

  * OAuth2 client-credentials grant (RFC 6749 §4.4) — the interoperable machine-to-machine shape, so
    a partner's HTTP client library already speaks it.
  * The client SECRET is high-entropy (>= 256 bits, a CSPRNG) and stored ONLY as an argon2/bcrypt
    hash (NIST SP 800-63B; OWASP ASVS 2.4). It is shown exactly once, at issue and at rotation, and
    exists in the clear nowhere else — not in the response of any other route, not in a log.
  * Authentication is TIMING-SAFE and reveals nothing: a wrong secret, an unknown client_id, and a
    revoked account are one answer (CWE-204 observable discrepancy; CWE-208 timing). One hash verify
    runs on every attempt, against a dummy hash when the client is unknown, so "does this client_id
    exist" is not an oracle.
  * LEAST PRIVILEGE (ISO 27001 A.8.2; OWASP ASVS 1.4.4): a service account can never hold a
    capability the human issuing it does not hold — `grantable_caps` refuses escalation (CWE-269) —
    and `platform.*` / `*` are stripped from any non-platform tenant's credential, the same last
    door user tokens pass through.
  * INDEPENDENT lifecycle: revoke or rotate a service account without touching any human account, so
    a partner integration can be cut off the moment a contract ends, and a personnel event never
    becomes an outage.
  * The token is an ORDINARY access JWT (the same issuer that signs user tokens), so all sixteen
    downstream services verify it on the path they already have — no second verification story. Its
    subject is a "svc_…" id that is visibly NOT a person in the audit trail (non-repudiation, ISO
    27001 A.8.15): the log distinguishes "the Odoo integration" from "Ahmed in accounting".
"""
from __future__ import annotations

import datetime as _dt
import secrets
from dataclasses import asdict, dataclass, field
from typing import Any

ACTIVE = "active"
REVOKED = "revoked"

# The public client_id is prefixed so it is recognisable as a machine principal wherever it lands —
# in a token subject, in an audit `actor`, in a config file on the partner's side.
_CLIENT_PREFIX = "svc_"
_COLL = "service_accounts"

# The datastore collection is exported for the store below and for the backup manifest.
COLLECTION = _COLL


class CapEscalation(Exception):
    """An issuer tried to grant a service account a capability the issuer does not hold."""

    def __init__(self, cap: str) -> None:
        super().__init__(cap)
        self.cap = cap


def new_client_id() -> str:
    # 128 bits of client_id entropy — unguessable, and distinct from any human subject namespace.
    return _CLIENT_PREFIX + secrets.token_hex(16)


def new_client_secret() -> str:
    # >= 256 bits from a CSPRNG. token_urlsafe(32) yields 43 URL-safe chars; there is nothing to
    # brute-force, and it is never stored in the clear, so no slow-KDF stretching is required at rest
    # beyond the argon2/bcrypt hash the hasher applies.
    return secrets.token_urlsafe(32)


def grantable_caps(requested: list[str], holder_caps, *, tenant: str,
                   platform_tenant: str) -> list[str]:
    """The caps an issuer holding `holder_caps` may put on a service account in `tenant`.

    Two rules, both boundaries rather than conveniences:
      1. NO ESCALATION (CWE-269): every requested cap must be one the issuer already holds, unless the
         issuer holds the `*` wildcard. A partner integration is granted a SUBSET of its creator's
         authority, never a superset — otherwise "issue a service account" would be a privilege-
         escalation primitive any tenant admin could use to mint themselves more power.
      2. NO PLATFORM AUTHORITY ON A TENANT CREDENTIAL: `*` and `platform.*` are stripped unless this
         IS the platform tenant — the same last door `AuthService._safe_caps` holds for humans, so a
         machine credential cannot carry cross-tenant power a human token could not.
    """
    holder = set(holder_caps or ())
    wildcard = "*" in holder
    granted: list[str] = []
    for cap in requested:
        if not isinstance(cap, str) or not cap:
            continue
        if not wildcard and cap not in holder:
            raise CapEscalation(cap)
        granted.append(cap)
    granted = _strip_platform(granted, tenant=tenant, platform_tenant=platform_tenant)
    # De-duplicate while preserving order, so a token claim is minimal and stable.
    seen: set[str] = set()
    return [c for c in granted if not (c in seen or seen.add(c))]


def _strip_platform(caps: list[str], *, tenant: str, platform_tenant: str) -> list[str]:
    pt = (platform_tenant or "").strip()
    if pt and tenant == pt:
        return list(caps)
    return [c for c in caps if c != "*" and not c.startswith("platform.")]


@dataclass
class ServiceAccount:
    """A machine identity. `id` IS the public client_id; the secret lives only as `secret_hash`."""

    id: str
    tenant: str
    name: str
    secret_hash: str
    caps: list[str] = field(default_factory=list)
    status: str = ACTIVE
    created: float = 0.0
    created_by: str = ""          # the pseudonymised human who issued it — for the audit trail
    last_used_at: float = 0.0     # 0 = never exchanged for a token
    # Rotation OVERLAP: the previous secret keeps authenticating until prev_secret_expires, so a
    # fleet embedding this credential (hundreds of tills that each update on their own schedule)
    # migrates across a window instead of failing the instant rotate is pressed. The window is a
    # TIMESTAMP that expires on its own — an overlap an operator must end manually is an overlap
    # nobody ends. Exactly ONE previous is ever kept; empty/0 means no overlap (an immediate cutover).
    prev_secret_hash: str = ""
    prev_secret_expires: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ServiceAccount":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})

    def public(self) -> dict[str, Any]:
        """Everything about the account EXCEPT its secret hash — safe for a list/detail response."""
        return {
            "client_id": self.id, "tenant": self.tenant, "name": self.name,
            "caps": list(self.caps), "status": self.status, "created": self.created,
            "last_used_at": self.last_used_at,
            # So an operator can SEE a rotation in progress: a non-zero value in the future means the
            # previous secret still authenticates until then. Never the hash — only when it lapses.
            "previous_secret_expires_at": self.prev_secret_expires,
        }


class ServiceAccountStore:
    """Persistence for service accounts — one id-keyed collection, no secondary index needed because
    the client_id IS the row id, so client-credentials auth is an O(1) get, never a scan."""

    def __init__(self, store) -> None:
        self._store = store

    async def create(self, account: ServiceAccount) -> ServiceAccount:
        await self._store.put(_COLL, account.id, account.to_dict())
        return account

    async def get(self, client_id: str) -> ServiceAccount | None:
        row = await self._store.get(_COLL, client_id)
        return ServiceAccount.from_dict(row) if row else None

    async def save(self, account: ServiceAccount) -> ServiceAccount:
        await self._store.put(_COLL, account.id, account.to_dict())
        return account

    async def list(self, tenant: str, *, limit: int = 100, after: str | None = None) -> list[dict]:
        # find() requires a non-empty filter — a service-account list is always tenant-scoped, so one
        # tenant can never enumerate another's machine identities (T1). Never a scan (safety:scans).
        rows, _ = await self._store.find(_COLL, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created", "desc"))
        return rows


def _now() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


class ServiceAccounts:
    """Issue, authenticate, rotate, and revoke machine identities."""

    def __init__(self, store: ServiceAccountStore, hasher) -> None:
        self._store = store
        self._hasher = hasher
        # A precomputed dummy hash so an unknown client_id costs the same as a real one (CWE-204).
        self._dummy_hash = hasher.hash("timing-equalizer")

    async def issue(self, *, tenant: str, name: str, caps: list[str], created_by: str = "",
                    on_commit=None) -> tuple[ServiceAccount, str]:
        """Create a service account and return it WITH its one-time raw secret.

        `caps` must already be the grantable set (the route applies `grantable_caps` against the
        issuing human's own caps). The secret is generated here, hashed, and never retained.

        `on_commit(account)`, if given, runs AFTER the account (with its client_id) is built but
        BEFORE it is persisted — the write-ahead seam the route uses to audit the issue naming the
        real client_id, so a credential the log could not record is never created.
        """
        secret = new_client_secret()
        account = ServiceAccount(
            id=new_client_id(), tenant=tenant, name=name,
            secret_hash=self._hasher.hash(secret), caps=list(caps),
            status=ACTIVE, created=_now(), created_by=created_by)
        if on_commit is not None:
            await on_commit(account)
        await self._store.create(account)
        return account, secret

    async def authenticate(self, client_id: str, client_secret: str) -> ServiceAccount | None:
        """The client-credentials check. Timing-safe; a wrong secret, an unknown client, and a
        revoked account are indistinguishable (all None → one 401).

        During a rotation OVERLAP the PREVIOUS secret is accepted too, but ONLY while its window is
        open — the comparison is the same constant-time verify, and an expired previous is never
        consulted, so it stops working on its own with no manual step."""
        account = await self._store.get(client_id) if client_id else None
        # ONE verify on every path — against the real hash or the dummy — so the presence of a
        # client_id is not observable through timing (CWE-204/CWE-208).
        ok = self._hasher.verify(client_secret or "",
                                 account.secret_hash if account is not None else self._dummy_hash)
        if account is not None and not ok and account.prev_secret_hash \
                and account.prev_secret_expires > _now():
            # The previous secret, accepted only inside the overlap window (constant-time).
            ok = self._hasher.verify(client_secret or "", account.prev_secret_hash)
        if account is None or not ok or account.status != ACTIVE:
            return None
        return account

    def token_claims(self, account: ServiceAccount, *, platform_tenant: str) -> dict[str, Any]:
        """The claims for a service-account access token. Caps are re-filtered at MINT (defence in
        depth: even if a stored account somehow held platform.*, the token would not), and the token
        is marked as a service actor so a log reader — and a service that wants to — can tell."""
        caps = _strip_platform(list(account.caps), tenant=account.tenant,
                               platform_tenant=platform_tenant)
        return {
            "caps": caps,
            "roles": [],                        # a machine holds capabilities, not human roles
            "tenant": account.tenant,
            "actor_type": "service",            # additive: downstream ignores it unless it looks
            "svc_name": account.name,           # a human label for the audit trail
        }

    async def rotate(self, account: ServiceAccount, *, overlap_seconds: float = 0.0) -> str:
        """Replace the secret, return the new one once. The client_id is unchanged.

        OVERLAP: with overlap_seconds > 0 the PREVIOUS secret keeps authenticating until it expires
        ON ITS OWN — so a fleet embedding this credential migrates across the window instead of every
        caller failing the instant rotate is pressed (a POS build on hundreds of tills each updates
        only when its shop next takes an update; a hard cutover is a fortnight of outage). An overlap
        an operator has to end by hand is an overlap nobody ends, so it is a timestamp, not a task.

        overlap_seconds <= 0 is an IMMEDIATE cutover — the right choice for a leak. And a second
        rotation inside a window keeps only ONE previous (the just-rotated secret), dropping the
        older: two live overlaps is a credential nobody can reason about, and a second rotation is
        usually somebody reacting to a leak, who wants the exposure to shrink, not chain.
        """
        secret = new_client_secret()
        if overlap_seconds and overlap_seconds > 0:
            account.prev_secret_hash = account.secret_hash          # the secret being rotated out
            account.prev_secret_expires = _now() + float(overlap_seconds)
        else:
            account.prev_secret_hash = ""                           # immediate cutover / leak response
            account.prev_secret_expires = 0.0
        account.secret_hash = self._hasher.hash(secret)
        await self._store.save(account)
        return secret

    async def revoke(self, account: ServiceAccount) -> ServiceAccount:
        """Cut the account off. The row is kept (so the audit trail still resolves the client_id to a
        name) but no secret authenticates and no token mints."""
        account.status = REVOKED
        return await self._store.save(account)

    async def touch(self, account: ServiceAccount) -> None:
        """Best-effort last-used stamp — useful for spotting a credential nobody uses any more, which
        is a credential that should be revoked (ISO 27001 A.8.2 access review)."""
        account.last_used_at = _now()
        await self._store.save(account)
