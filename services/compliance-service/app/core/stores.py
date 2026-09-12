"""Repositories over the generic DataStore. Consent proof is append-only."""
from __future__ import annotations

from typing import Any

from app.core.models import (OPEN_DSAR_STATUSES, Breach, Consent, ConsentProof, Dsar,
                             DataTarget, LegalDocument, LegalHold, ProcessingActivity)
from app.ports.datastore import DataStore

_CONSENT = "consent"          # "<tenant>::<subject>::<purpose>"
_LEGAL = "legal_documents"    # "<tenant>::<doc_id>::<version>"
_LEGAL_CURRENT = "legal_current"   # "<tenant>::<doc_id>" -> pointer at the current published version
_PROOF = "consent_proof"      # append-only
_DSAR = "dsar"
_HOLDS = "legal_holds"
_ROPA = "processing_activities"
_BREACH = "breaches"
_TARGETS = "data_targets"     # the data map
_PROOF_SEQ = "consent_proof_seq"   # (tenant, subject) -> monotonic counter; see add_proof()
_SEQ_WIDTH = 12               # zero-padded so the id sorts as TEXT in both adapters

# Every read below is scoped to ONE tenant, and the scope is pushed into the datastore. It used
# to be applied in Python after reading up to a million rows of every tenant's consent records,
# DSARs and breach reports — the three collections a privacy regulator would ask about first.
#
# A note on ordering. The shipped btree index covers `created`; these collections order by
# `created_at` / `discovered_at` / `at`. The GIN containment still narrows to the tenant's own
# rows before the sort, so the cost is O(that tenant's records), not O(everyone's). It is not
# yet O(page). Where that matters, add the field to the adapter's `_INDEXES` — and match the
# COALESCE expression exactly, or Postgres will silently ignore the index.


class ConsentStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def normalise(purpose: str) -> str:
        """`"  Matching "` -> `"matching"`. The one place the rule lives.

        Purpose was raw free text used directly as a store key, so `Matching` and `matching` were
        two different consents. A caller asking with the wrong casing got `no_consent_record` —
        IDENTICAL to the answer for a person who genuinely never consented. The failure is a
        default-DENY, so it is not an unlawful-processing hole; it is undiagnosable, which is its
        own kind of bad. Nobody can tell "they said no" from "you typed it differently".

        Only case and surrounding whitespace. Not punctuation or internal spaces: `data-sharing`
        and `data sharing` are plausibly different purposes chosen deliberately, and collapsing
        them would merge two consents into one — a far worse error than failing to match them.
        """
        return purpose.strip().casefold()

    @classmethod
    def _key(cls, tenant: str, subject: str, purpose: str) -> str:
        return f"{tenant}::{subject}::{cls.normalise(purpose)}"

    @staticmethod
    def _legacy_key(tenant: str, subject: str, purpose: str) -> str:
        """The pre-normalisation key: purpose exactly as it was typed."""
        return f"{tenant}::{subject}::{purpose}"

    async def get(self, tenant: str, subject: str, purpose: str) -> Consent | None:
        """Normalised first, then the raw key a pre-normalisation row was written under.

        The fallback is what makes this safe to deploy without a migration. Rewriting live consent
        records in bulk is the riskiest operation available here — a bug in that pass silently
        changes what the platform believes it is allowed to do — so instead old rows are found
        where they are, and put() retires them the next time that consent is written.
        """
        row = await self._store.get(_CONSENT, self._key(tenant, subject, purpose))
        if row is None:
            legacy = self._legacy_key(tenant, subject, purpose)
            if legacy != self._key(tenant, subject, purpose):
                row = await self._store.get(_CONSENT, legacy)
        return Consent.from_dict(row) if row else None

    async def put(self, c: Consent) -> Consent:
        """Write normalised, and delete any legacy twin.

        The delete is the important half. Without it a tenant with a legacy `Matching` row that
        grants `matching` ends up with TWO rows for one consent, and which one answers depends on
        lookup order — the exact ambiguity this change exists to remove. Lazy migration: each
        consent is converted the next time it is written, and nothing is touched before then.
        """
        key = self._key(c.tenant, c.subject, c.purpose)
        legacy = self._legacy_key(c.tenant, c.subject, c.purpose)
        row = c.to_dict()
        # Stamp the CANONICAL key as the row id. The consent key normalises the purpose (casefold),
        # so it is not a plain template of the row's raw fields — carrying it as `id` is what lets
        # the backup core restore each consent under exactly the key it was stored under. Ignored by
        # from_dict (it reads only dataclass fields), invisible to every reader.
        row["id"] = key
        await self._store.put(_CONSENT, key, row)
        if legacy != key:
            await self._store.delete(_CONSENT, legacy)
        return c

    async def migrate_purpose_keys(self, limit: int = 1000) -> dict[str, Any]:  # atomic: ok — one-shot migration, single runner
        """Rewrite pre-normalisation rows under their normalised key. Run once, deliberately.

        The lazy fallback in get() is NOT sufficient, and a test is what proved it: it only finds
        a legacy row when the caller happens to use the original spelling. A caller asking
        `matching` cannot find a row stored as `Matching`, because casefold has no inverse — so
        for the very caller this change exists to help, the drift survives until the row is
        rewritten. Hence a real migration, exposed as an explicit operator action rather than run
        at boot, because it edits records that decide what the platform may lawfully do.

        CONFLICTS ARE NOT MERGED. If both spellings exist, a withdrawal wins over a grant —
        deny beats allow whenever we are unsure, since the cost of wrongly denying is a refused
        request and the cost of wrongly allowing is processing someone objected to. Where both
        agree, the most recently updated survives. Every conflict is REPORTED, so a resolution
        nobody chose is at least a resolution somebody can see.
        """
        # sweeper: converting every consent key IS visiting every consent — a range/shape question
        # the store cannot express, since it speaks equality only. Explicit scan_all rather than
        # find({}) so it is visible to the next reader and to safety:scans. Operator-triggered,
        # never on a request path, capped per run.
        rows = await self._store.scan_all(_CONSENT, limit=limit)
        migrated = 0
        conflicts: list[dict[str, str]] = []

        for row in rows:
            tenant = row.get("tenant", "")
            subject = row.get("subject", "")
            purpose = row.get("purpose", "")
            legacy = self._legacy_key(tenant, subject, purpose)
            key = self._key(tenant, subject, purpose)
            if legacy == key:
                continue                              # already normalised

            existing = await self._store.get(_CONSENT, key)
            if existing is not None:
                winner = self._resolve(existing, row)
                winner["id"] = key          # carry the canonical key (see put) after a merge
                conflicts.append({"tenant": tenant, "subject": subject, "purpose": purpose,
                                  "kept": "withdrawn" if not winner.get("granted", True)
                                          else "most_recent"})
                await self._store.put(_CONSENT, key, winner)
            else:
                row["purpose"] = self.normalise(purpose)
                row["id"] = key             # carry the canonical key (see put)
                await self._store.put(_CONSENT, key, row)
            await self._store.delete(_CONSENT, legacy)
            migrated += 1

        return {"scanned": len(rows), "migrated": migrated, "conflicts": conflicts}

    @staticmethod
    def _resolve(a: dict, b: dict) -> dict:
        """Deny beats allow; otherwise the newer record wins."""
        if not a.get("granted", True):
            return a
        if not b.get("granted", True):
            return b
        return a if float(a.get("updated_at") or 0) >= float(b.get("updated_at") or 0) else b

    async def for_subject(self, tenant: str, subject: str, limit: int = 100) -> list[dict]:
        rows, _ = await self._store.find(_CONSENT, {"tenant": tenant, "subject": subject},
                                         limit=limit, sort=("updated_at", "desc"))
        return rows

    # ── append-only proof ──────────────────────────────────────────────────────
    async def add_proof(self, p: ConsentProof) -> ConsentProof:
        """Append a proof, stamping an id that sorts by INSERTION order.

        `proofs()` orders by `at`, and the datastore breaks ties on `id` to keep the order total
        — without a tie-break, two rows sharing a sort value can straddle a page boundary and one
        is silently lost. But a random uuid makes that tie-break ARBITRARY. A grant and a
        withdrawal recorded in the same clock tick then come back in random order, and the
        history handed to a regulator says the subject withdrew consent before they gave it.

        The old code never noticed because Python's sort is stable and the rows arrived in
        insertion order. Pushing the sort into the datastore removed that accident. So the id
        now carries a zero-padded per-subject counter: same tick, same `at`, still the order it
        was written in.
        """
        seq_key = f"{p.tenant}::{p.subject}"
        # ATOMIC per-subject counter. The get-then-put let two concurrent proofs for one subject both
        # read n=N and both write n=N+1 — the SAME insertion-order key on both, so the tamper-evident
        # order the regulator relies on (grant-before-withdraw) forks. cas retries on a lost race so
        # each proof gets a distinct, monotonic sequence.
        seq = 1
        for _ in range(64):
            row = await self._store.get(_PROOF_SEQ, seq_key)
            ver = int(row.get("version", 0)) if row else 0
            seq = int((row or {}).get("n", 0)) + 1
            if await self._store.cas(_PROOF_SEQ, seq_key, ver, {"n": seq}):
                break
        else:
            raise RuntimeError("could not allocate a consent-proof sequence under contention")
        p.id = f"{seq:0{_SEQ_WIDTH}d}::{p.id}"
        await self._store.put(_PROOF, p.id, p.to_dict())
        return p

    async def proofs(self, tenant: str, subject: str, limit: int = 100) -> list[dict]:
        """The append-only consent proof for one subject, oldest first.

        This is the evidence produced when a regulator asks "prove they consented". Reading it
        used to mean reading every OTHER subject's proof too.
        """
        rows, _ = await self._store.find(_PROOF, {"tenant": tenant, "subject": subject},
                                         limit=limit, sort=("at", "asc"))
        return rows


class DsarStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, d: Dsar) -> Dsar:
        await self._store.put(_DSAR, d.id, d.to_dict())
        return d

    async def get(self, dsar_id: str) -> Dsar | None:
        row = await self._store.get(_DSAR, dsar_id)
        return Dsar.from_dict(row) if row else None

    async def open_for(self, tenant: str, subject: str, dsar_type: str) -> Dsar | None:
        """An identical open request -> return it (idempotent, no double-processing).

        The equality filter cannot say "status NOT IN (...)", and inverting a negative into a
        positive list is how a new status silently becomes "open". So the OPEN statuses are
        named explicitly in the model and pushed down; anything not named is not open.
        """
        rows, _ = await self._store.find(
            _DSAR, {"tenant": tenant, "subject": subject, "type": dsar_type,
                    "status": list(OPEN_DSAR_STATUSES)},
            limit=1, sort=("created_at", "asc"))
        return Dsar.from_dict(rows[0]) if rows else None

    async def open_dsars(self, limit: int = 100, after: str | None = None) -> tuple[list[Dsar], str | None]:
        """Every still-open request, oldest first — the sweeper's work list.

        Deliberately NOT tenant-scoped: the sweeper is a platform job driving every tenant's
        erasures, and a per-tenant loop would first need to enumerate tenants (a scan) to find the
        work. Filtered on the same explicit OPEN_DSAR_STATUSES as open_for() and pushed down to the
        store, so this stays an indexed find; oldest-first means whatever is closest to its SLA
        deadline is retried first.
        """
        rows, cursor = await self._store.find(
            _DSAR, {"status": list(OPEN_DSAR_STATUSES)}, limit=limit, after=after,
            sort=("created_at", "asc"))
        return [Dsar.from_dict(r) for r in rows], cursor

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_DSAR, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("created_at", "desc"))
        return rows


class HoldStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, h: LegalHold) -> LegalHold:
        await self._store.put(_HOLDS, h.id, h.to_dict())
        return h

    async def get(self, hold_id: str) -> LegalHold | None:
        row = await self._store.get(_HOLDS, hold_id)
        return LegalHold.from_dict(row) if row else None

    async def active_for(self, tenant: str, subject: str, limit: int = 100) -> list[LegalHold]:
        """Holds that block an erasure. Read on every DSAR delete — a fail-CLOSED path.

        If this read is truncated the service concludes "no hold" and erases data it was
        legally required to keep. It is scoped, and the `active` flag is pushed down.
        """
        rows, _ = await self._store.find(
            _HOLDS, {"tenant": tenant, "subject": subject, "active": True},
            limit=limit, sort=("placed_at", "asc"))
        return [LegalHold.from_dict(r) for r in rows]

    async def active_for_subject(self, tenant: str, subject: str,
                                 limit: int = 1000) -> tuple[list[LegalHold], bool]:
        """Active holds blocking an erasure for ONE subject, AND whether the answer is COMPLETE.

        The subject-scoped sibling of active_in_tenant(). erase() decides whether it may delete a
        subject's data from this, so the completeness flag is the whole safety argument: a silently
        truncated list must be visible as INCOMPLETE and force a fail-CLOSED refusal, rather than
        collapse to a false "no hold" and erase data under litigation hold. `active` is pushed down
        to the store (never filtered in Python over a mixed active/released page), so the limit
        budgets ACTIVE holds only."""
        rows, cursor = await self._store.find(
            _HOLDS, {"tenant": tenant, "subject": subject, "active": True},
            limit=limit, sort=("placed_at", "asc"))
        return [LegalHold.from_dict(r) for r in rows], cursor is None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_HOLDS, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("placed_at", "desc"))
        return rows

    async def active_all(self, limit: int = 100,
                         after: str | None = None) -> tuple[list[dict], str | None]:
        """Every tenant's ACTIVE legal hold, oldest-first — the platform compliance-officer view.
        Deliberately NOT tenant-scoped (mirrors DsarStore.open_dsars): the `active` flag is pushed
        down to the store, so this stays an indexed find, not a platform-wide scan. Platform-scoped:
        gated to platform.compliance at the route + audited."""
        rows, cursor = await self._store.find(_HOLDS, {"active": True}, limit=limit, after=after,
                                              sort=("placed_at", "asc"))
        return rows, cursor

    async def active_in_tenant(self, tenant: str, limit: int) -> tuple[list[LegalHold], bool]:
        """Every active hold for a tenant, and whether the answer is COMPLETE.

        The second half of that tuple is the whole safety argument. A caller uses this to decide
        what it may delete; a silently truncated list means it deletes data that is held. So the
        cursor is inspected and completeness returned as a fact, rather than the caller inferring
        it from `len(rows) < limit` — which is the same guess, made further from the evidence.
        """
        rows, cursor = await self._store.find(
            _HOLDS, {"tenant": tenant, "active": True}, limit=limit, sort=("placed_at", "asc"))
        return [LegalHold.from_dict(r) for r in rows], cursor is None


class TargetStore:
    """The data map — which services hold which category of a subject's data."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def register(self, t: DataTarget) -> DataTarget:
        await self._store.put(_TARGETS, t.id, t.to_dict())
        return t

    async def for_tenant(self, tenant: str, limit: int = 1_000) -> list[DataTarget]:
        """The tenant's data map. Drives erasure fan-out: a missing target is data left behind."""
        rows, _ = await self._store.find(_TARGETS, {"tenant": tenant}, limit=limit,
                                         sort=("service", "asc"))
        return [DataTarget.from_dict(r) for r in rows]

    async def get(self, target_id: str) -> DataTarget | None:
        row = await self._store.get(_TARGETS, target_id)
        return DataTarget.from_dict(row) if row else None

    async def delete(self, target_id: str) -> None:
        await self._store.delete(_TARGETS, target_id)


class RopaStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, a: ProcessingActivity) -> ProcessingActivity:
        await self._store.put(_ROPA, a.id, a.to_dict())
        return a

    async def get(self, activity_id: str) -> ProcessingActivity | None:
        row = await self._store.get(_ROPA, activity_id)
        return ProcessingActivity.from_dict(row) if row else None

    async def delete(self, activity_id: str) -> None:
        await self._store.delete(_ROPA, activity_id)

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_ROPA, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("name", "asc"))
        return rows


class LegalPageStore:
    """Versioned legal/content pages, plus an O(1) pointer at each doc's current published version.

    The pointer is a separate row, not a scan for `status==published` ordered by version: the
    post-auth gate reads "what is current" on every login, and a version string like "1.10" does
    not sort after "1.9" as text — so 'the latest' must be a fact we WRITE at publish time, not one
    we try to derive by sorting."""
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _vkey(tenant: str, doc_id: str, version: str) -> str:
        return f"{tenant}::{doc_id}::{version}"

    @staticmethod
    def _ckey(tenant: str, doc_id: str) -> str:
        return f"{tenant}::{doc_id}"

    async def put_version(self, doc: LegalDocument) -> LegalDocument:
        key = self._vkey(doc.tenant, doc.doc_id, doc.version)
        row = doc.to_dict()
        row["id"] = key            # id==key so the backup fan-out restores it under its exact key
        await self._store.put(_LEGAL, key, row)
        return doc

    async def get_version(self, tenant: str, doc_id: str, version: str) -> LegalDocument | None:
        row = await self._store.get(_LEGAL, self._vkey(tenant, doc_id, version))
        return LegalDocument.from_dict(row) if row else None

    async def set_current(self, tenant: str, doc_id: str, version: str,
                          requires_acceptance: bool) -> None:
        key = self._ckey(tenant, doc_id)
        await self._store.put(_LEGAL_CURRENT, key, {
            "id": key, "tenant": tenant, "doc_id": doc_id, "version": version,
            "requires_acceptance": requires_acceptance})

    async def current_pointer(self, tenant: str, doc_id: str) -> dict | None:
        return await self._store.get(_LEGAL_CURRENT, self._ckey(tenant, doc_id))

    async def current(self, tenant: str, doc_id: str) -> LegalDocument | None:
        ptr = await self.current_pointer(tenant, doc_id)
        if not ptr:
            return None
        return await self.get_version(tenant, doc_id, ptr["version"])

    async def versions(self, tenant: str, doc_id: str, limit: int = 100) -> list[dict]:
        rows, _ = await self._store.find(_LEGAL, {"tenant": tenant, "doc_id": doc_id},
                                         limit=limit, sort=("created_at", "desc"))
        return rows

    async def documents(self, tenant: str, limit: int = 200) -> list[dict]:
        """The current pointer for every doc in the tenant — what the gate and the panel list."""
        rows, _ = await self._store.find(_LEGAL_CURRENT, {"tenant": tenant},
                                         limit=limit, sort=("doc_id", "asc"))
        return rows

    async def all_doc_ids(self, tenant: str, limit: int = 2000) -> list[str]:
        """Every doc_id with AT LEAST ONE version, published or not. documents() only sees a doc once
        it has a current (published) pointer — a freshly-drafted, never-published type is invisible to
        it — so an authoring panel that shows work in progress reads this instead, over the raw
        version rows (_LEGAL) rather than the pointer table."""
        rows, _ = await self._store.find(_LEGAL, {"tenant": tenant},
                                         limit=limit, sort=("doc_id", "asc"))
        seen: list[str] = []
        for row in rows:
            doc_id = row.get("doc_id")
            if doc_id and doc_id not in seen:
                seen.append(doc_id)
        return sorted(seen)


class BreachStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, b: Breach) -> Breach:
        await self._store.put(_BREACH, b.id, b.to_dict())
        return b

    async def get(self, breach_id: str) -> Breach | None:
        row = await self._store.get(_BREACH, breach_id)
        return Breach.from_dict(row) if row else None

    async def list(self, tenant: str, limit: int = 100, after: str | None = None) -> list[dict]:
        rows, _ = await self._store.find(_BREACH, {"tenant": tenant}, limit=limit, after=after,
                                         sort=("discovered_at", "desc"))
        return rows
