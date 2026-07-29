"""Person and Membership — one login, many companies, and a wall that does not move.

A freelancer works for two companies. They want one login. They must NEVER get one view: Company A
seeing Company B's work is a leak and probably a contract breach. So the person gets a key to two
doors and can only ever stand in one room at a time.

    Person      one login, global          identifier, password, MFA — and NO authority
    Membership  person x tenant x roles    the key to ONE door
    Token       person + EXACTLY ONE tenant

WHY THE PERSON CARRIES NO CAPS.

The freelancer is an admin at their own two-person company and a read-only contractor at the bank.
Put `caps` on the Person and they are an admin AT THE BANK. That is not a bug you fix later; it is
a privilege escalation across a company boundary (CWE-269).

So `Person` has no `caps` field and no `roles` field. Not empty ones — none. The mistake is made
UNREPRESENTABLE rather than merely discouraged, and `safety:tenancy` asserts the class never grows
one. Authority lives on the Membership, and nowhere else.

WHY THE STORE IS KEYED (person, tenant).

The membership lookup happens on every mint — login, tenant switch, and every refresh. It is a
keyed O(1) get, never a scan over memberships. `safety:scans` would refuse a scan on a request
path anyway, and this is the request path that decides whether one company can read another's
data. It is not a place to be clever.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.core.models import _now, new_id

ACTIVE, SUSPENDED, PENDING = "active", "suspended", "pending"

# A person an admin created but who has not set a password yet. No password hashes to this, so no
# password authenticates as them — the sentinel is checked BEFORE the hasher, because a hasher
# handed a non-hash may raise, and a 500 on the login path is an oracle that says "this identifier
# exists and is unclaimed".
UNUSABLE_PASSWORD = "!unusable"

_PERSONS = "persons"
_MEMBERSHIPS = "memberships"          # key: "<person_id>::<tenant>"  -> O(1), never scanned
_PERSON_INDEX = "persons_by_identifier"


@dataclass
class Person:
    """A human. One login, global.

    Deliberately has NO caps and NO roles. See the module docstring: authority on the Person is a
    cross-company privilege escalation, so the field does not exist to be filled in by mistake.
    """
    id: str
    identifier: str                  # the normalized login identifier (see LoginIdentifier port)
    password_hash: str
    status: str = ACTIVE             # active | suspended
    mfa_secret: str | None = None
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "identifier": self.identifier, "password_hash": self.password_hash,
            "status": self.status, "mfa_secret": self.mfa_secret,
            "created_at": self.created_at, "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Person":
        return cls(id=d["id"], identifier=d["identifier"], password_hash=d["password_hash"],
                   status=d.get("status", ACTIVE), mfa_secret=d.get("mfa_secret"),
                   created_at=d.get("created_at", _now()), updated_at=d.get("updated_at", _now()))

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "identifier": self.identifier, "status": self.status}


@dataclass
class Membership:
    """The key to ONE door. All authority lives here, scoped to exactly one tenant."""
    person_id: str
    tenant: str
    caps: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    status: str = ACTIVE             # active | suspended
    created_at: str = field(default_factory=_now)

    @property
    def active(self) -> bool:
        return self.status == ACTIVE

    @property
    def id(self) -> str:
        """The row's unique, stable identity — and the SORT TIE-BREAK.

        A membership is one person in one tenant, so (person_id, tenant) already identifies it.
        This is not decoration: paging is keyset-ordered by `created_at`, which is NOT unique —
        grant two memberships in the same instant and they tie. The datastore breaks that tie on
        `id`, and a row with no `id` ties at "" against every other row, so the cursor cannot
        advance past the collision. The page silently drops members, and a directory that quietly
        omits people is worse than one that fails.
        """
        return f"{self.person_id}::{self.tenant}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "person_id": self.person_id, "tenant": self.tenant,
                "caps": self.caps, "roles": self.roles, "status": self.status,
                "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Membership":
        return cls(person_id=d["person_id"], tenant=d["tenant"],
                   caps=list(d.get("caps") or []), roles=list(d.get("roles") or []),
                   status=d.get("status", ACTIVE), created_at=d.get("created_at", _now()))

    def public(self) -> dict[str, Any]:
        """What a person may see about their OWN membership. Never anyone else's."""
        return {"tenant": self.tenant, "roles": self.roles, "status": self.status}


class PersonExists(Exception):
    """The identifier is already taken by a DIFFERENT human. Never merge — see T7."""


class MembershipStore:
    def __init__(self, store) -> None:
        self._store = store

    # ── persons ─────────────────────────────────────────────────────────────
    @staticmethod
    def _mkey(person_id: str, tenant: str) -> str:
        return f"{person_id}::{tenant}"

    async def create_person(self, p: Person) -> Person:
        # The identifier is GLOBALLY unique once a person can span tenants. Two different humans
        # who happen to share an email must NOT be merged into one login (T7) — that hands one of
        # them the other's employer. Refuse; a refusal is an operator's afternoon, a merge is a
        # breach nobody notices.
        if await self._store.get(_PERSON_INDEX, p.identifier) is not None:
            raise PersonExists(f"identifier already registered: {p.identifier}")
        await self._store.put(_PERSONS, p.id, p.to_dict())
        await self._store.put(_PERSON_INDEX, p.identifier, {"id": p.id})
        return p

    async def person(self, person_id: str) -> Person | None:
        row = await self._store.get(_PERSONS, person_id)
        return Person.from_dict(row) if row else None

    async def person_by_identifier(self, identifier: str) -> Person | None:
        idx = await self._store.get(_PERSON_INDEX, identifier)
        return await self.person(idx["id"]) if idx else None

    async def save_person(self, p: Person) -> Person:
        """Persist an EXISTING person. Never touches the identifier index — a rename is a
        different operation with a different threat model, and it is not this one."""
        p.updated_at = _now()
        await self._store.put(_PERSONS, p.id, p.to_dict())
        return p

    async def delete_person(self, p: Person) -> None:
        await self._store.delete(_PERSON_INDEX, p.identifier)
        await self._store.delete(_PERSONS, p.id)

    # ── memberships ─────────────────────────────────────────────────────────
    async def grant(self, m: Membership) -> Membership:
        await self._store.put(_MEMBERSHIPS, self._mkey(m.person_id, m.tenant), m.to_dict())
        return m

    async def membership(self, person_id: str, tenant: str) -> Membership | None:
        """O(1). Called on EVERY mint — login, tenant switch, and every refresh."""
        row = await self._store.get(_MEMBERSHIPS, self._mkey(person_id, tenant))
        return Membership.from_dict(row) if row else None

    async def revoke(self, person_id: str, tenant: str) -> None:
        """Remove the key to one door. The Person is untouched — they still work elsewhere."""
        await self._store.delete(_MEMBERSHIPS, self._mkey(person_id, tenant))

    async def memberships(self, person_id: str, limit: int = 100) -> list[Membership]:
        """The person's own doors. Pushed down by person_id — never a scan of everyone's."""
        rows, _ = await self._store.find(_MEMBERSHIPS, {"person_id": person_id},
                                         limit=limit, sort=("created_at", "asc"))
        return [Membership.from_dict(r) for r in rows]

    async def has_other_active_owner(self, tenant: str, person_id: str, *,
                                     page: int = 200, max_pages: int = 50) -> bool:
        """Is there an ACTIVE owner of this tenant who is NOT this person?

        The sole-owner guard's question. It stops at the FIRST other owner found, so the common
        case is one bounded page; it only walks further when a tenant genuinely has few owners. The
        tenant is pushed into the datastore (never a platform-wide scan) — `safety:scans` holds.
        A False answer must be TRUSTWORTHY: it is what refuses to strand a tenant, so the walk is
        exhaustive within its page bound rather than sampling one page and guessing.
        """
        if not tenant:
            raise ValueError("has_other_active_owner requires a tenant")
        after = None
        for _ in range(max_pages):
            rows, after = await self._store.find(_MEMBERSHIPS, {"tenant": tenant}, limit=page,
                                                 after=after, sort=("created_at", "asc"))
            for r in rows:
                m = Membership.from_dict(r)
                if m.person_id != person_id and "owner" in m.roles and m.active:
                    return True
            if not after:
                break
        return False

    async def members(self, tenant: str, *, limit: int,
                      after: str | None = None) -> tuple[list[Membership], str | None]:
        """Everyone who works HERE — one tenant, paginated.

        Identity owns who works where. Without this, every product service grows its own user
        directory: a second copy of a person, and a second person to erase when they ask to be
        forgotten.

        The tenant is pushed into the datastore, never filtered in Python — a filter in Python is
        a scan of every membership on the platform, and `safety:scans` refuses it on a request
        path. `after` is the opaque keyset cursor; it is never an offset.
        """
        if not tenant:
            # An empty `where` would match everything — every membership in every company. There
            # is no legitimate caller for that, so it is not a query, it is an accident.
            raise ValueError("members() requires a tenant — an empty filter is every company")
        rows, cursor = await self._store.find(_MEMBERSHIPS, {"tenant": tenant},
                                              limit=limit, after=after,
                                              sort=("created_at", "asc"))
        return [Membership.from_dict(r) for r in rows], cursor
