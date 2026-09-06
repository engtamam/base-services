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
# A verified ERASURE waiting out its cooling-off window (ERASURE_GRACE_DAYS) before dispatch, so an
# admin has a real window to place a legal hold first. Distinct from in_progress: nothing has been
# told to erase yet, and a hold placed now still refuses it. Open (see OPEN_DSAR_STATUSES).
SCHEDULED = "scheduled"

# The statuses in which a DSAR is still OPEN, named POSITIVELY.
#
# The idempotency lookup used to ask `status not in (completed, refused)`. Expressed that way, a
# status added later is open by default: a `cancelled` DSAR would be returned as the caller's
# existing open request, and the new one would never be created. Listing the open states means a
# new status is closed until someone decides otherwise. It is also the only form the datastore's
# equality filter can push down.
OPEN_DSAR_STATUSES = (PENDING, VERIFIED, SCHEDULED, IN_PROGRESS)


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


# Who is legally entitled to give this consent. The platform-wide assumption used to be that the
# subject always speaks for themself; splitting the two is what makes guardianship expressible.
SELF = "self"
GUARDIAN = "guardian"
LEGAL_REPRESENTATIVE = "legal_representative"
CONSENT_BASES = (SELF, GUARDIAN, LEGAL_REPRESENTATIVE)


@dataclass
class Consent:
    """Consent is per (subject, purpose) — purpose limitation is the law, not a nicety.

    `subject` is who the data is ABOUT. `consented_by` is who actually agreed. They are the same
    person in the ordinary case and deliberately separable in the ones that matter: a parent for a
    child, a carer under a court order, an attorney under a power of attorney, an executor for an
    estate. Modelling it as CAPACITY rather than as "minors" is what makes one mechanism cover all
    of them — and what stops the next jurisdiction needing a new field.
    """
    tenant: str
    subject: str
    purpose: str
    granted: bool = False
    notice_version: str = ""          # WHICH privacy notice they accepted
    method: str = ""                  # how it was collected (web_form, api, import…)
    expires_at: float = 0.0           # 0 = no expiry
    updated_at: float = 0.0
    # ── capacity (Phase 39) ────────────────────────────────────────────────
    basis: str = SELF                 # self | guardian | legal_representative
    consented_by: str = ""            # who agreed; empty means the subject themself
    relation: str = ""                # parent | custodian | power_of_attorney | executor…
    subject_born_at: float = 0.0      # 0 = UNKNOWN, which is not the same as "adult"

    def status_at(self, now: float, majority_at: float | None = None) -> str:
        """WHY this consent does or does not permit processing, at `now`.

        THE SINGLE HOME OF THE RULE. This used to be a bool named `active()` with no production
        caller, while consent_service restated the same granted/expiry test inline because it
        needed WHICH reason, and a bool cannot carry one. Two copies of the rule that decides
        whether processing is lawful, one of them dead and therefore free to rot — mutating the
        careful copy broke no test.

        Returning the reason rather than a bool is what lets one implementation serve both callers,
        so there is nothing left to duplicate.

        `majority_at` is the day a delegated consent lapses, passed in rather than computed here:
        the age of majority is jurisdiction policy and belongs in CapacityPolicy, not on a model.
        Passing None means "capacity not evaluated", which is the pre-policy case and must not be
        silently read as "no lapse".
        """
        if not self.granted:
            return "withdrawn"
        if self.expires_at and now >= self.expires_at:
            return "expired"
        # A DELEGATED consent whose subject has reached majority is spent, whether or not anyone
        # ever wrote an expiry onto the row. Rows granted before the policy existed carry
        # expires_at=0 and would otherwise answer "granted" for ever.
        if majority_at and self.basis != SELF and now >= majority_at:
            return "lapsed_at_majority"
        return "granted"

    def active(self, now: float, majority_at: float | None = None) -> bool:
        """The bool form, for callers that do not need the reason."""
        return self.status_at(now, majority_at) == "granted"

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
    actor: str = ""                   # who OPERATED the system (an admin, an API caller)
    ip: str = ""
    at: float = 0.0
    # WHOSE consent this legally is, which is a different question from who typed it in. "We had
    # consent" is not an answer without "from whom, and were they entitled to give it" — that is
    # the half a regulator asks about, and the half that was missing.
    basis: str = SELF
    consented_by: str = ""
    relation: str = ""

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
    # The KIND of identifier `subject` is — a pseudonym, a phone hash, an internal id. Optional and ""
    # by default (unchanged behaviour: a target erases the verbatim value as before). When set, it
    # travels on the wire so a target that indexes by a DIFFERENT kind can REFUSE explicitly rather
    # than match zero rows and ack a clean "erased" — a false completion indistinguishable from a real
    # one. It is metadata ABOUT the subject, never a second value to match on.
    subject_kind: str = ""
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
    # For a verified erasure under a grace window: the UTC time dispatch may begin. 0 = no grace (or
    # not an erasure) → dispatch immediately, today's behaviour. Set once, when the erasure is first
    # processed after verification; the sweep dispatches it when now >= dispatch_at.
    dispatch_at: float = 0.0
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


DRAFT, PUBLISHED = "draft", "published"


@dataclass
class LegalSection:
    """One structured block of a legal document, so a viewer renders STRUCTURE — not raw HTML it
    would have to trust. `heading` may be empty (a lead paragraph); `body` is plain text."""
    heading: str = ""
    body: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LegalDocument:
    """One VERSION of a legal/content page (privacy policy, terms, a cookie notice…).

    A document is identified by a stable `doc_id` slug and carries many versions over time. A
    PUBLISHED version is IMMUTABLE — you publish a NEW version to change the text — because an
    acceptance proof points at the exact version the person agreed to, and rewriting it under their
    feet would make that proof a lie. Only a draft is editable.

    Bilingual by construction — but NOT by storing a separate copy per language. `content` holds ONE
    set of rich blocks (`{kind: {…}}`, each localizable field carrying `{en, ar}`) plus a `title`
    ({en, ar}) and a `values` map for the `{{PLACEHOLDER}}` slots. The SAME renderer the pack was
    authored in (the UI's DocBlocksView) resolves the tokens from `values` and picks the language at
    read time, so a table stays a table, an Arabic reader gets Arabic, and the two editions can never
    drift apart. `requires_acceptance` is what the post-auth gate reads to decide whether a fresh
    publish must be re-accepted before the person may proceed.
    """
    tenant: str
    doc_id: str                       # slug: "privacy-policy", "terms-of-service", …
    version: str                      # caller-chosen, monotonic by convention: "1.0", "2026-08-10"
    status: str = DRAFT               # draft | published
    requires_acceptance: bool = False
    # {"title": {en, ar}, "blocks": [ {kind: {...}}, ... ], "values": {PLACEHOLDER: value}}
    content: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    published_at: float = 0.0
    effective_at: float = 0.0         # 0 = effective on publish
    default_locale: str = "en"

    def view(self, locale: str = "") -> dict[str, Any]:
        """The viewer payload: the rich blocks, the placeholder values, and the title in the asked
        language (falling back so a half-translated title is never blank). The blocks themselves are
        bilingual — the renderer picks the language — so there is nothing to select here but the
        title."""
        loc = locale or self.default_locale
        title = self.content.get("title", {})
        if isinstance(title, dict):
            title = title.get(loc) or title.get(self.default_locale) or next(iter(title.values()), "")
        return {"doc_id": self.doc_id, "version": self.version, "locale": loc,
                "title": title, "blocks": self.content.get("blocks", []),
                "values": self.content.get("values", {}),
                "requires_acceptance": self.requires_acceptance,
                "effective_at": self.effective_at or self.published_at}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LegalDocument":
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
