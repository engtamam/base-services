"""Domain models — Consent, DSAR, LegalHold, ProcessingActivity (RoPA), Breach, DataTarget."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# DSAR types (PDPL/GDPR data-subject rights)
ACCESS, EXPORT, ERASURE, CORRECTION = "access", "export", "erasure", "correction"
DSAR_TYPES = (ACCESS, EXPORT, ERASURE, CORRECTION)

# DSAR lifecycle. `completed` is DERIVED from per-target acks, never set by hand.
PENDING, VERIFIED, IN_PROGRESS, COMPLETED, REFUSED = (
    "pending", "verified", "in_progress", "completed", "refused")

# The statuses in which a DSAR is still OPEN, named POSITIVELY.
#
# The idempotency lookup used to ask `status not in (completed, refused)`. Expressed that way, a
# status added later is open by default: a `cancelled` DSAR would be returned as the caller's
# existing open request, and the new one would never be created. Listing the open states means a
# new status is closed until someone decides otherwise. It is also the only form the datastore's
# equality filter can push down.
OPEN_DSAR_STATUSES = (PENDING, VERIFIED, IN_PROGRESS)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Consent:
    """Consent is per (subject, purpose) — purpose limitation is the law, not a nicety."""
    tenant: str
    subject: str
    purpose: str
    granted: bool = False
    notice_version: str = ""          # WHICH privacy notice they accepted
    method: str = ""                  # how it was collected (web_form, api, import…)
    expires_at: float = 0.0           # 0 = no expiry
    updated_at: float = 0.0

    def active(self, now: float) -> bool:
        if not self.granted:
            return False
        return not (self.expires_at and now >= self.expires_at)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Consent":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class ConsentProof:
    """Append-only evidence. Never mutated — an auditor must be able to reconstruct history."""
    id: str
    tenant: str
    subject: str
    purpose: str
    action: str                       # granted | withdrawn
    notice_version: str = ""
    method: str = ""
    actor: str = ""
    ip: str = ""
    at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class DataTarget:
    """Where a subject's data lives — the data map that makes erasure COMPLETE."""
    id: str
    tenant: str
    service: str
    category: str                     # e.g. profile, invoices, files
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DataTarget":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Dsar:
    id: str
    tenant: str
    subject: str
    type: str = ACCESS
    status: str = PENDING
    verified: bool = False            # identity proven BEFORE any PII is released
    verification_token: str = ""
    verification_expires: float = 0.0
    targets: dict[str, bool] = field(default_factory=dict)   # "service:category" -> acked?
    # Per-target dispatch record: attempts, last outcome, next retry, and the ack BODY (what was
    # erased, what was kept and why). `targets` above stays the single authority on completion;
    # this is the evidence and the retry schedule beside it.
    target_state: dict[str, Any] = field(default_factory=dict)
    dispatched: bool = False
    refusal_reason: str = ""
    payload: dict[str, Any] = field(default_factory=dict)     # correction data / export bundle
    created_at: float = 0.0
    deadline_at: float = 0.0          # SLA (UTC)
    completed_at: float = 0.0

    def is_complete(self) -> bool:
        """Completion is DERIVED: every target acknowledged. A partial delete is not 'done'."""
        return bool(self.targets) and all(self.targets.values())

    def derive_completion(self, now: float) -> bool:
        """Apply the derivation. Returns True if this call is what completed the request.

        This lives on the MODEL because a DSAR finishes by two different routes — a target acking
        synchronously through the dispatcher, or an async target pushing its ack to /target-ack —
        and the rule must be identical on both. It was not: completion was applied only on the push
        path, so a DSAR whose targets all answered on the first dispatch stayed `in_progress` for
        ever. Every service reported success, every target really had erased, and the request never
        closed. One rule, one place, called from both.
        """
        if self.status in (COMPLETED, REFUSED) or not self.is_complete():
            return False
        self.status = COMPLETED
        self.completed_at = now
        return True

    def overdue(self, now: float) -> bool:
        return self.status not in (COMPLETED, REFUSED) and bool(self.deadline_at) and now > self.deadline_at

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def public(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("verification_token", None)      # never expose the token in listings
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Dsar":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class LegalHold:
    id: str
    tenant: str
    subject: str
    reason: str = ""
    active: bool = True
    placed_at: float = 0.0
    released_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LegalHold":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class ProcessingActivity:
    """RoPA — Records of Processing Activities (PDPL art. / GDPR art. 30)."""
    id: str
    tenant: str
    name: str
    purpose: str = ""
    lawful_basis: str = "consent"     # consent | contract | legal_obligation | vital | public | legitimate
    data_categories: list[str] = field(default_factory=list)
    retention_days: int = 0           # 0 = keep while the purpose lasts
    residency: str = "SA"
    cross_border: bool = False
    recipients: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ProcessingActivity":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Breach:
    id: str
    tenant: str
    description: str = ""
    discovered_at: float = 0.0
    notify_deadline_at: float = 0.0   # discovered + BREACH_NOTIFY_HOURS (UTC)
    notified_at: float = 0.0
    affected_subjects: int = 0
    data_categories: list[str] = field(default_factory=list)
    severity: str = "medium"

    def notified(self) -> bool:
        return self.notified_at > 0

    def overdue(self, now: float) -> bool:
        return not self.notified() and bool(self.notify_deadline_at) and now > self.notify_deadline_at

    def hours_remaining(self, now: float) -> float:
        return max(0.0, (self.notify_deadline_at - now) / 3600.0) if not self.notified() else 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Breach":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
