# Phase 17 — feature-flag-service v2: correctness, hot-path & experiments

> **Why this phase exists:** Phase 16 planned only to `saas-checklist.md` §17's feature
> bullets. Two defects and four gaps came from that. This phase fixes them and encodes a
> new rule: **the checklist is the floor, not the ceiling** (see memory `phase-doc-rules`).

---

## 0. Definition of done (Phase 17)

### 🔴 Defects (must fix — these are bugs, not features)
- [ ] **D1 — unwired config knob.** `FLAG_CACHE_TTL_SECONDS` is declared and never consumed; the `Cache` port is unused. Wire it: cache flag definitions with the TTL, invalidate on `flag.changed`.
- [ ] **D2 — write amplification on the hot path.** `touch()` writes the flag row on **every** evaluation → `evaluate_all` with N flags = N reads + N writes **per request**. Flag evaluation is the highest-QPS endpoint in the platform. Only persist `last_evaluated` when it is older than `LAST_SEEN_WRITE_THRESHOLD_SECONDS` (default 3600). Target: **~0 writes/request** steady-state.

### 🟠 Correctness
- [ ] **C1 — `bucket_by`.** Rollouts currently bucket by user (falling back to tenant). A schema/infra rollout must be **per-tenant** (every user of a company sees the same thing); a UI test is **per-user**. Add `bucket_by: user|tenant|attr:<name>`; default `user`. Getting this wrong ships half a company onto a new code path.

### 🟢 Production capability the checklist omits
- [ ] **E1 — Multivariate variants with weights.** `{control: 50, treatment: 50}` assigned from the **same deterministic bucket**. Weights must sum to 100 (validated). Returns `variant` + `reason=variant_rollout`.
- [ ] **E2 — Exposure events.** Emit `flag.exposed {key, variant, subject, bucket_by, reason, at}` on evaluation (sampled by `EXPOSURE_SAMPLE_PCT`) so analytics can **measure** an experiment. Without this, A/B is decorative. Never blocks evaluation.
- [ ] **E3 — Segments.** A named, reusable audience (`beta_users` = rules) referenced by many flags (`segments: ["beta_users"]`). Defined once; without it, rules get copy-pasted and rot.
- [ ] **E4 — Prerequisites.** `prerequisites: [{key, variant?}]` — flag B is off unless flag A is on (safe dependent rollouts). Cycle-safe (depth-capped, cycle detected → fail-closed).
- [ ] **E5 — Scheduled flag changes.** `start`/`end` on a flag (we already do this for maintenance). Outside the window → `default`, reason `scheduled`.
- [ ] **E6 — Change audit with actor + diff.** `flag.changed` carries `actor` + `before`/`after` so audit-service can answer "who turned this on, from what".
- [ ] **E7 — Maintenance bypass allowlist.** Admin subjects / health checks reach the system *during* maintenance — otherwise you can't fix what you're fixing.

### Non-functional gates (new — required in every future phase doc)
- [ ] **Hot path**: `evaluate_all` does **0 datastore writes** and ≤1 cached read in steady state; a benchmark asserts it.
- [ ] **Every config knob has a consumer** — a test greps `Settings` fields and fails on any unused one.
- [ ] `pytest` + `ci/security_gate.py` green; live boot proves each item.

---

## 1. Design notes

**Cache (D1).** `Cache` port keyed `flag:<key>` and `flags:all`, TTL `FLAG_CACHE_TTL_SECONDS`. Invalidate on any write (`upsert`/`archive`/override) and on the `flag.changed` event (so replicas converge). A cache miss falls through to the provider — never to "off".

**Last-seen (D2).** `touch()` becomes `maybe_touch()`: read the cached/loaded row's `last_evaluated`; write only if `now - last_evaluated > LAST_SEEN_WRITE_THRESHOLD_SECONDS`. Stale detection only needs hour-grain. Steady state: one write per flag per hour, not per request.

**bucket_by (C1).** `subject = ctx.user | ctx.tenant | ctx.attrs[name]`. If the chosen subject is missing → **not in rollout** (fail-safe, never "half on"). The bucket hash already includes the flag key; `bucket_by` only changes *what* is hashed.

**Variants (E1).** Reuse `bucket(salt, key, subject)` → `0..9999`. Walk the weight table in declared order, accumulating `weight*100`; the first band containing the bucket wins. Same subject → same variant, forever, no storage. A boolean flag is just `{off:X, on:Y}` — we keep the boolean API and layer variants on top.

**Exposure (E2).** Emitted on the EventBus (fire-and-forget, never raises, sampled). Contains **no PII beyond the subject id the caller already supplied**.

**Precedence (updated).** `unknown_flag → kill_switch → scheduled → prerequisite_failed → entitlement_denied → user_override → tenant_override → segment_match → rule_match → variant_rollout | rollout → default`.

---

## 2. Steps

| Task | Step |
|---|---|
| 17.1 | **D2** `maybe_touch` + `LAST_SEEN_WRITE_THRESHOLD_SECONDS`; hot-path write test |
| 17.2 | **D1** wire `Cache` (definitions + `flags:all`), invalidate on write/event; cached-read test |
| 17.3 | **C1** `bucket_by` (user/tenant/attr) + fail-safe missing subject |
| 17.4 | **E1** weighted variants (validated weights) + `variant_rollout` reason |
| 17.5 | **E2** exposure events (sampled, fire-and-forget) |
| 17.6 | **E3** segments (model, store, `segment_match`) |
| 17.7 | **E4** prerequisites (cycle-safe) + **E5** scheduled windows |
| 17.8 | **E6** change audit (actor + before/after) + **E7** maintenance bypass allowlist |
| 17.9 | Routes + manifest for segments / variants / bypass |
| 17.10 | Tests (incl. hot-path benchmark + unused-config-knob guard) |
| 17.11 | Verify + live boot |
| 17.12 | Docs (README precedence table, memory) |

---

## 3. Out of scope
Streaming/SSE push (poll + TTL + `flag.changed` is enough) · approval workflows · experiment statistics (analytics-service computes; we only emit exposures).
