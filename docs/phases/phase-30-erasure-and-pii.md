# Phase 30 — Erasure: deleting a person without destroying the record

A tenant asks to delete their account. Two obligations collide:

* **Privacy law** (PDPL Art. 5 / GDPR Art. 17) says erase the person's data.
* **Tax law** (ZATCA, 6–10 years) says keep the invoice. Security says keep the audit trail.

Both are mandatory. So "delete the row" is not the answer, and neither is "keep everything."

**What we have today.** compliance-service already models this correctly: a legal hold beats an
erasure, completion is DERIVED from per-target acks (never declared), identity is verified before
any PII is released, and an `Orchestrator` port fans the request out.

**What we do not have.** *Nobody implements the receiving side.* No service has an erase handler.
A DSAR would fan out and sit incomplete forever. It does not lie — it simply never finishes.

**And one thing that cannot be fixed by adding a handler.** `AuditEvent` stores `actor`, `ip`, and
free-form `metadata`, and every event is **hash-chained**. Redact one field and `verify()` fails
from that seq onward. The tamper-evidence *is the product*. You cannot delete a person out of a
hash chain — which means the design is wrong at the point of WRITE, not at the point of erasure.

---

## 1. Definition of Done

### (1) Is this on the hot path?

Erasure is not — it is rare, async, and orchestrated. **But the fix to the chain problem is.**

If immutable records must store a pseudonym instead of an identity, then *every audit write*
needs a `subject_ref`. Resolving identity → pseudonym must be **O(1) and add zero datastore reads**
to the audit ingest path, or we have traded a privacy bug for a throughput bug.

A pseudonym is a pure function — `HMAC(pii_salt, tenant, subject)` — so it needs no lookup at all
in the write direction. Only the *reverse* (pseudonym → person) needs a stored mapping, and that
is read on a DSAR, not on the hot path. **Benchmark: audit ingest gains 0 reads and 0 writes.**

### (2) Does every config knob have a consumer?

New knobs: `PII_SALT` (see below — it is FROZEN), `RETENTION_<category>_DAYS`, `ERASURE_SLA_DAYS`.
Each needs a real consumer and the standard knob-guard test. A retention period that is documented
and never enforced is the defect `coverage:services` exists to catch — and here it is also a
compliance finding.

**`PII_SALT` is a FROZEN identifier.** Change it and every pseudonym in every immutable record
re-maps: the audit history's `subject_ref`s become orphans that can never be linked to anyone
again, and erasure can no longer prove it erased the right person. It belongs in
`config/product.identifiers.lock.json` beside `signatureDomain` and `rolloutSalt`, guarded by
`safety:product`. This falls out of machinery we already built.

### (3) What production needs that "add a delete endpoint" omits

* **Derived stores leak after the source is erased.** The search index, the analytics rollups, the
  caches. Erase the user row and the search index still answers their name. Every derived store
  needs an invalidation path, and it must be part of the ack.
* **Backups.** Nobody rewrites backups; it is not feasible and regulators do not expect it. The
  honest answer is a *stated* policy: backups expire on a fixed window, erasure is applied on
  restore, and the window is disclosed. Silence here is the finding.
* **Erasure must be idempotent and resumable.** It spans N services; one will be down.
* **"Erased" must be provable.** A DSAR that says completed must be able to show which target
  acked when. We already derive completion from acks — extend it to record the evidence.

### (4) The correctness footgun

**An incomplete data map lets a DSAR report `completed` while the person's data is still there.**

Completion is derived from acks of the targets compliance *knows about* (`DataTarget` registry).
A service that holds subject data but was never registered is never asked, never acks, and is
never missed — so the derivation says "all targets acked" and the DSAR closes green. The person is
told they were erased. They were not.

This is the exact shape of every defect this repo has chased: **the check passes because it was
looking at an incomplete list.** The map cannot be trusted because someone remembered to update
it. It must be **verified against the code** by a gate.

---

## 2. The design

### PII never enters an immutable record

| Store | Mutable? | What it may hold |
| --- | --- | --- |
| identity, tenant, notification prefs | yes | the real identity (name, email) — **erasable** |
| audit chain | **no** | `subject_ref` (pseudonym) only. Never `actor` as an email, never raw `ip` |
| billing / wallet ledgers, invoices | **no** | `subject_ref` + the financial facts |
| ZATCA e-invoices | **no** (already sent) | whatever the tax authority requires, retained |
| analytics events | yes | `subject_ref`; de-identified on erasure |
| search index, caches, rollups | derived | rebuilt/invalidated on erasure |

`subject_ref = HMAC(PII_SALT, tenant, subject_id)` — stable, per-tenant, computed at write with
no lookup. The **linkage** (`subject_ref` → person) lives in exactly ONE place.

**Erasure = delete the mutable PII + destroy the linkage.** The chain never changes; `verify()`
still passes on every historical event. The audit record still proves *that* someone did
something, in the right order, untampered — it just can no longer name them. That is precisely
what both laws want.

### Every service that holds subject data implements the receiving side

An `Erasure` port + `POST /internal/privacy/erase` (internal token), which:
1. erases or de-identifies its own subject data,
2. invalidates its derived stores,
3. **acks compliance** with what it did and its legal basis for anything retained.

Anything retained must name its basis (`tax_retention`, `legal_hold`, `security_audit`), so
"we kept it" is always an answer with a reason attached, never a silence.

---

## 3. Steps

| # | Step |
| --- | --- |
| 30.1 | `pii.py`: `subject_ref = HMAC(PII_SALT, tenant, subject)`. Pure, no lookup. |
| 30.2 | `PII_SALT` added to the FROZEN identifier lock; `safety:product` guards it |
| 30.3 | audit-service: stop storing raw `actor`/`ip`; store `subject_ref`. Migration note: old events keep old values (chain is immutable — that is the point) |
| 30.4 | `Erasure` port + `/internal/privacy/erase` in every service holding subject data |
| 30.5 | Derived-store invalidation (search re-index, analytics de-id, cache bust) inside the ack |
| 30.6 | Retention config per category + enforcement (a documented period nobody enforces is a finding) |
| 30.7 | compliance: record ack EVIDENCE (who acked, when, what was retained and on what basis) |
| 30.8 | **GATE `safety:pii`** — every service that stores a subject identifier MUST have an erase handler and a registered `DataTarget`. Verified against the CODE, not a hand-kept list |
| 30.9 | Backup policy: stated retention window, erasure-on-restore, disclosed |
| 30.10 | Tests: erasure completes; a held subject is refused; the audit chain still verifies AFTER erasure; a service that forgets its handler FAILS the gate |

## 4. Out of scope

Rewriting the ZATCA e-invoices already submitted to the tax authority. They are sent; they are the
authority's record now. We retain them and say so.

## 5. The one that will bite

`verify()` must still pass **after** an erasure — that is the whole test that proves this design
works. If it does not, we have either broken the audit chain (lost tamper-evidence) or failed to
erase (lost the legal case). Step 30.10 asserts both in one test, and it is the reason the
pseudonym has to be at WRITE time and not a retrofit.

---

**STATUS: 🟢 CORE BUILT.** 30.1, 30.2, 30.4, 30.8, 30.10 done.

The decisive test passes: record 20 audit events for a subject, erase the subject, and the chain
STILL verifies — because it never held the identity, only the pseudonym. `test_erasure_chain.py`
asserts both halves in one file: the person is unlinkable AND `verify()` is intact.

`safety:pii` found **7 services holding a data subject with no erase handler** on its first run.
Every one was real (`user_overrides`, `owner`, `user`, `subject` — all persisted). All 7 now
erase, and every retention names a basis from a closed set.

**Still open:** 30.3 (audit call sites must pass `subject_ref`, not a raw actor — `assert_no_pii`
enforces it but the callers are not yet migrated), 30.5 derived-store invalidation on the ack path,
30.6 retention-period enforcement, 30.7 ack evidence recorded by compliance, 30.9 backup policy.
