# Phase 47 — admin-action completeness: every operator action a panel can do

## The belief this phase is built to destroy

> "The admin panel shows what someone remembered to declare."

Today each service's `admin_actions.py` is a hand-curated list, and the panel renders exactly it —
no more. That means the panel's power is bounded by memory, not by what the service can actually do.
The recurring failure is the one backup-service had before its rich pass: an operator surface exists
in code (capability-gated routes), works, is audited — and the panel cannot see it, because nobody
added the declaration. A panel that can change a plan's price but cannot list prices, or trigger a
job but not see the queue, is *half a panel*, and the missing half is invisible.

So the organising rule is: **`admin_actions` ⊇ every operator-capable route, reads and writes.** If an
operator can do it through the API with a `platform.*` / `*.manage` capability, a UI must be able to
render it — and if the only way to do it today is an internal-token or cron route, that is itself the
gap to close (promote it to a capability-gated operator route, the way backup-service did in Phase 46).

## What "rich enough" means — the completeness bar every task is measured against

A service passes this phase when, for its own domain:

1. **Every operator WRITE has a declared action.** Walk `app/api/v1/**` and any `operator.py` — every
   route guarded by a `platform.*` / `*.manage` capability (not `_SELF`, not internal-token) is
   declared.
2. **Every write has a READ beside it.** An operator must be able to see state before changing it —
   list the credentials before rotating one, see the queue before draining it, view the limit before
   raising it. This is the "half a panel" rule: a panel that only acts is dangerous.
3. **Params carry `help`, destructive actions carry `confirm`/`danger`,** and truly dangerous ones
   **echo-confirm at the API** (repeat the id / type a word), so a mis-click or a replay cannot fire
   them. Grouped into sensible `group`s; reads declared before writes within a group.
4. **No secret is readable back.** A "view credential" read shows a hint, never the value
   (`safety:no-secret-reads` already enforces this; the declaration must not imply otherwise).
5. **Every declared write is audited** (`safety:audit-wiring`), and its `auditable_actions` entry
   exists with the right `critical` flag.

Where a genuine operator action exists ONLY as an internal-token or cron route (e.g. a sweep, a
recompute, a reindex), the task adds a capability-gated operator route for it — the same
internal-for-cron / operator-for-panel split backup-service now uses — rather than declaring an
internal route as an admin action (which the gates correctly forbid).

## How each task is done, and verified

For one service at a time (never a blanket sweep — the whole point is reviewing ACTUAL routes):

1. Enumerate the real operator surface: `grep` the `@router` decorators under `app/api/v1/**` and
   filter to `platform.*` / `*.manage` capabilities.
2. For each, confirm a declaration exists in `admin_actions.py`; add the missing ones richly (params,
   help, confirm/danger, group).
3. Add the missing READS so every write has a "see current state" companion.
4. Promote any panel-worthy internal-token/cron action to a capability-gated operator route.
5. Wire: `manifest.py` endpoints + capabilities, `auditable_actions.py` for new writes, regenerate
   OpenAPI.
6. Prove: `safety:admin` (structure), `safety:admin-params` (declaration matches the live API),
   `safety:audit-wiring`, `safety:manifest-routes`, `safety:openapi` all green; new routes tested for
   capability-gating and echo-confirm.

## Outcome (all 22 complete)

Every service reviewed against the completeness bar. Twelve had a real gap and were enriched; ten
were verified complete as-is (their empty/limited surface a reviewed decision, not a leftover).
Platform-wide declared admin actions rose to 274, each proven by `safety:admin` + `safety:admin-params`.

Gaps found and closed:
- **47.1 wallet** — cross-tenant operator reads (balance/ledger/reconcile), platform-only, audited.
- **47.2 onboarding** — flow catalogue, provisioning-run view, resume stuck cleanup.
- **47.3 audit** — the operator export (search was declared, export wasn't).
- **47.4 search** — an index catalogue (you can't inspect an index you can't discover).
- **47.5 file** — operator file browse/inspect; `platform.files` was enforced but unadvertised.
- **47.6 job** — a job-detail read (see-before-you-replay), route-shadowing guard.
- **47.7 analytics** — edit a saved report in place (create+delete ≠ update).
- **47.8 identity** — change a member's roles in place, respecting the T2 credential boundary.
- **47.9 tenant** — the tenant's own admin console (tenant.manage) declared.
- **47.10 authz** — view a policy, and a subject's EFFECTIVE capabilities (was internal-only).
- **47.12 notification** — the delivery log ("did it send?"), recipients masked.
- **47.17 zatca** — an invoice-detail read (see why it was rejected before resubmitting).
- **47.20 stream** — the rest of the export flow + session playback.

Verified complete as-is (reviewed decision): 47.11 assistant, 47.13 verification (enumeration-oracle
guard), 47.14 api-gateway (panel host, empty by design), 47.15 feature-flag, 47.16 connector,
47.18 webhook, 47.19 compliance (list already returns full detail), 47.21 billing (drift already
fixed in v0.10.x), 47.22 backup (the reference implementation).

Recurring shapes: a **half-a-panel** (list/act declared, but not the companion read or the rest of a
flow), an **intent-in-code / no-route** capability (delivery log, `platform.files`, authz effective),
and a **see-before-you-act** detail read behind a mutating action (job/zatca).

## Definition of Done (the whole phase)

- Every service meets the completeness bar above; the sum of declared actions reflects the real
  operator surface, not a remembered subset.
- No panel action renders a form the API rejects, and no operator route is undiscoverable to the
  panel (both directions checked by `safety:admin-params`).
- Reads-before-writes holds per service: no write without a companion read.
- All existing gates stay green; new operator routes are capability-gated, audited, and echo-confirm
  where destructive.
- Non-functionals: no new unwired config knob; no secret readable back; every new route rate-limited
  like its siblings; no cross-tenant read added without the platform-capability + audit pattern.

## Tasks — one per service, thinnest coverage first (highest value)

Counts are current `admin_actions` declarations; the candidate areas are starting points, not the
answer — each task reviews the service's real routes.

- [ ] **47.1 wallet-service** (2 → rich). Candidate operator surface: view a tenant/subject balance
  and ledger; manual credit/debit adjustment (danger, audited critical); place/release a hold; view
  and set per-tenant limits; freeze/unfreeze an account; low-balance threshold; reconciliation view.
- [ ] **47.2 onboarding-service** (2 → rich). Has `/admin/inspect` and `/admin/force/{subject}`
  already. Add: list flow definitions and steps; view a tenant's onboarding state; restart/advance/
  reset a flow (confirm); the reads for each.
- [ ] **47.3 audit-service** (3 → rich). Read-heavy by nature: search/query the log; verify chain
  integrity on demand; export a range; view retention policy and set it; log statistics. (Writes are
  few and mostly policy — mind the `critical` flags.)
- [ ] **47.4 search-service** (3 → rich). Reindex / rebuild an index (danger, long-running → operator
  route over the cron); view index stats and health; manage synonyms/analyzers/config; purge an index
  (danger, echo-confirm).
- [ ] **47.5 file-service** (6 → richer). Has `operator.py`. Add/verify: view and set quotas; storage
  stats; force a virus scan; list and abort large/stalled uploads; delete a file (danger); retention
  policy.
- [ ] **47.6 job-service** (8 → richer). List jobs and queues; cancel / retry / requeue a job
  (confirm); pause/resume a queue; inspect and replay the dead-letter queue; view the scheduler; force
  a reclaim. Reads for each.
- [ ] **47.7 search/analytics reads** — **analytics-service** (9). Dashboards/report config; trigger a
  recompute (operator route over the cron); export; retention; view pipeline health.
- [ ] **47.8 identity-service** (9 → richer). Suspend/reactivate a user; force logout / revoke
  sessions; reset MFA; view login history and active sessions; unlock a locked account. Each write
  audited critical; reads beside them.
- [ ] **47.9 tenant-service** (9 → richer). List tenants and view one; suspend/activate a tenant
  (danger); set plan/limits; view usage; transfer/rename. Reads first.
- [ ] **47.10 authz-service** (10 → richer). List roles and capabilities; grant/revoke a role or
  capability (audited critical); view who-holds-what; manage policy. The read "who has this power" is
  as important as the grant.
- [ ] **47.11 assistant-service** (9, 7 routes). Verify each operator route is declared; add reads for
  model/provider/policy config and any test/preview action.
- [ ] **47.12 notification-service** (8). Providers and chains (has operator surface); templates
  (list/edit/preview); a test-send; view delivery status and failures; per-tenant overrides.
- [ ] **47.13 verification-service** (8). Providers/config; view verification attempts; force/expire a
  verification (confirm); per-tenant provider overrides; the reads.
- [ ] **47.14 api-gateway** (0). Likely legitimately thin, but confirm: route/upstream health view,
  rate-limit policy view/set, any operator toggle. If genuinely none, document the empty list as a
  reviewed decision (not a leftover), as the template comment requires.
- [ ] **47.15 feature-flag-service** (16). Already rich; audit for completeness — every flag/segment/
  rollout write has a read, and any evaluation-debug/inspect route is declared.
- [ ] **47.16 connector-service** (15). Audit: connector CRUD, credentials (write-only), sync trigger
  + status, per-tenant enablement, the reads.
- [ ] **47.17 zatca-service** (15). Audit: certificate/CSR lifecycle, submission status and retry,
  environment/config, view submissions — each with a read.
- [ ] **47.18 webhook-service** (19). Audit: endpoint CRUD, secret rotation (write-only), delivery
  view + replay, per-tenant enablement.
- [ ] **47.19 compliance-service** (26). Audit: DSAR management, retention policy, erasure status,
  legal-hold — confirm every write has a read and the destructive ones echo-confirm.
- [ ] **47.20 stream-service** (29). Audit: channel/stream lifecycle, subscriber management, replay/
  retention — confirm completeness.
- [ ] **47.21 billing-service** (28). Audit: the recent price-listing and invoice-listing gaps closed
  in v0.10.x proved this service drifts; confirm every plan/coupon/subscription/invoice/settlement
  operator action has a read and is declared.
- [ ] **47.22 backup-service** (20). Done in Phase 46's rich pass — verify it still meets the bar as
  the reference implementation other tasks copy the pattern from.

## Why one service per task (not a codemod)

The gate `safety:admin-params` already proves a declaration matches the API; it cannot tell you a
real operator route is MISSING a declaration — absence is invisible to it, exactly like the
undeclared-holder and unwired-knob failures elsewhere in this codebase. Only reading each service's
actual routes finds the gap, so each task is a review, and its evidence is the diff between "routes an
operator can call" and "actions the panel can render" going to zero.
