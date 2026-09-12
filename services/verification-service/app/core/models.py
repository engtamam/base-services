"""Domain models — a verification is a CLAIM, its EVIDENCE, and a VERDICT.

Someone asserts something about themselves: "this national id is mine", "I control commercial
registration 1010…", "this VAT number belongs to my company". The service's whole job is to turn
that assertion into an answer somebody else can rely on, and to keep the evidence for the answer.

Two modelling decisions carry most of the weight.

**The claim TYPE is open, not an enum of Saudi values.** `individual`, `business`, `tax`,
`document`, `contact` are the shapes a claim can take anywhere; which provider answers them is
config. Baking `nafath` or `wathq` into the type would make every other market a special case, and
this platform ships to whoever buys it.

**The raw identifier is NEVER stored.** A national id or a CR number is exactly the data an
attacker wants, and a verification store is a concentrated pile of it. What is stored is a salted
hash — enough to recognise the same identity again, useless for impersonating anyone — plus the
verdict. The store cannot leak identities even if it is read in full, which is a property no access
control can give you.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# What is being claimed. Open by design: a market we have never heard of adds a provider, not a
# new type. `document` is the universal fallback — a human looking at an uploaded certificate
# works everywhere and needs no external contract at all.
INDIVIDUAL, BUSINESS, TAX, DOCUMENT, CONTACT = (
    "individual", "business", "tax", "document", "contact")
CLAIM_TYPES = (INDIVIDUAL, BUSINESS, TAX, DOCUMENT, CONTACT)

# The verdict. `refused` and `error` are DIFFERENT and must never merge:
#
#   refused  the provider answered, and the answer was no. The person is not who they claim.
#   error    we could not ask. We know nothing.
#
# Collapsing them picks one of two failures with no way back. Treat error as refused and a provider
# outage locks out every legitimate customer; treat refused as error and you retry forever against
# an answer that will not change — or worse, fall through to "allow" and admit the fraudster. The
# split is the reason this enum exists rather than a boolean.
PENDING, VERIFIED, REFUSED, EXPIRED, ERROR = (
    "pending", "verified", "refused", "expired", "error")
VERDICTS = (PENDING, VERIFIED, REFUSED, EXPIRED, ERROR)

# Verdicts that mean "this claim is currently good". Named POSITIVELY, for the same reason
# OPEN_DSAR_STATUSES is: a verdict added later is NOT trusted until someone decides it should be.
TRUSTED_VERDICTS = (VERIFIED,)


def new_id(prefix: str = "") -> str:
    import uuid
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Evidence:
    """WHY we believe the verdict. Kept even after the claim itself is erased.

    A verdict with no evidence is an opinion. This records who answered, what reference they gave
    us, and when — so an auditor can go back to the provider and check, and so a dispute has
    something to examine other than our own say-so.
    """
    provider: str = ""                # which adapter answered ("wathq", "manual", "null")
    reference: str = ""               # the provider's own id for this check, for later audit
    checked_at: float = 0.0
    detail: str = ""                  # why it was refused, or why we could not ask
    reviewer: str = ""                # for manual review: WHO decided (a pseudonym)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Verification:
    """One claim, its current verdict, and the evidence behind it."""
    id: str
    tenant: str
    subject: str                      # who made the claim (a pseudonym at the edge)
    claim_type: str = INDIVIDUAL
    # The salted hash of what was claimed — never the national id or CR itself. Stable, so the
    # same identity is recognisable across tenants and across deletion; one-way, so it names
    # nobody. This is what makes once-per-identity anti-abuse possible without keeping identities.
    claim_ref: str = ""
    verdict: str = PENDING
    evidence: dict[str, Any] = field(default_factory=dict)
    # A verdict is a snapshot, not a permanent fact: a commercial registration can be cancelled,
    # a licence can lapse. 0 = never expires, which is only right for claims that cannot change.
    expires_at: float = 0.0
    # Set when the provider answers asynchronously and we are waiting for a callback. A pending
    # verification that never resolves must expire rather than hang forever — but expiring is not
    # a verdict of verified.
    pending_until: float = 0.0
    provider_state: dict[str, Any] = field(default_factory=dict)   # opaque, for the callback
    created_at: float = 0.0
    updated_at: float = 0.0
    # Optimistic-lock counter for compare-and-set on a decision. Seeded to 1 on first write so a
    # version-pinned update is well-defined; each successful cas advances it.
    version: int = 1

    def is_trusted(self, now: float) -> bool:
        """Can a caller rely on this RIGHT NOW?

        Expiry is checked here rather than by a sweeper, because a stale verdict must read as
        untrusted the moment it lapses — not whenever a cron next runs. A sweeper that has not
        fired yet would otherwise leave a cancelled registration reading as verified.
        """
        if self.verdict not in TRUSTED_VERDICTS:
            return False
        return not (self.expires_at and now >= self.expires_at)

    def public(self) -> dict[str, Any]:
        """What a caller may see. `claim_ref` and `provider_state` never leave the service.

        claim_ref is a hash, but it is a STABLE one: handing it out would let a caller correlate
        the same person across tenants, which is precisely the linkage the hashing exists to
        prevent. provider_state can carry provider tokens.
        """
        d = asdict(self)
        d.pop("claim_ref", None)
        d.pop("provider_state", None)
        d.pop("version", None)          # the lock counter is internal, not part of the API shape
        return d

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Verification":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
