# Phase 46 — backup & disaster recovery

A new service, `backup-service`, plus one small seam added to every existing service. The goal is
the one that DR almost always fails to reach: **a restore that has been proven to work before you
need it.**

## The one belief this phase is built to destroy

> "We have backups."

Most platforms that say this discover, at the worst possible moment, that the backups were never
restorable — a collection was silently skipped, a service was down during the run, the format
drifted, or nobody ever tried a restore. Every record was internally consistent and the whole was
collectively useless. That is the erasure-receipt shape again, and it is the shape this phase
refuses.

So the organising rule is: **an unverified backup is treated as no backup at all.** A backup that
has not been test-restored and compared does not count toward coverage, does not silence an alert,
and is not something an operator may rely on.

## Plug-and-play: how a service joins, and why it cannot be forgotten

A service is NEVER reached into directly. backup-service does not open another service's database —
that would break the self-contained rule and would require it to understand twenty different
schemas. Instead each service exposes what it alone understands.

Three parts, each matching a contract already in the repo:

1. **Declare.** Each service adds a `backup` block to its manifest — the collections it owns, its
   object-store prefix, and whether each collection is tenant-scoped. This is the same declared-data
   pattern as `admin_actions`, `auditable_actions` and `events`: a pure literal, read as data.

2. **Two internal endpoints, shipped by the template.** `POST /internal/backup/export` streams the
   service's declared data, sealed; `POST /internal/backup/import` restores it idempotently. The
   service knows its own shape; backup-service only orchestrates.

3. **A gate makes the declaration honest.** `safety:backup-coverage` fails the build when a service
   writes a collection its manifest does not declare — exactly as `safety:manifest-routes` does for
   routes. This is the whole plug-and-play guarantee: **declared coverage equals real coverage,
   mechanically.** A new service is backed up the day it ships, or CI goes red the same day.

Discovery is the **manifest fan-out already used for the DSAR erasure sweep**. A holder nobody
declared is never asked to back up — and the gate makes not-declaring impossible to get away with.

## A THIRD-PARTY service can join by implementing a contract, not by being one of ours

This is the point that makes the backup service worth building rather than buying. An adopter's own
services — the ones they wrote, in whatever language — must be able to be backed up by the same
orchestrator, on the same schedule, with the same verified-restore guarantee. Nothing about the
design assumes a service is Python, is FastAPI, or shares our code. It assumes a service can answer
two HTTP calls and publish a manifest.

So the backup contract is PUBLIC, documented, and small:

- `GET /internal/manifest` returns a `backup` block — the collections/datasets it owns, and whether
  each is tenant-scoped. A third-party service publishes the same shape.
- `POST /internal/backup/export` streams its data as a sealed, checksummed archive in the documented
  envelope format. HOW it reads its own database is entirely its business.
- `POST /internal/backup/import` restores that archive idempotently, keyed on row identity.

That is the entire integration surface. A team registers their service's base URL and internal
token with backup-service (the same way billing registers a wallet URL), and from that moment it is
discovered, scheduled, backed up, verified and restorable exactly like a first-party service. They
write two endpoints once; they never write a backup system.

The envelope format and the two endpoint schemas ship as a versioned spec in `docs/contracts/`, so
a third party builds against a fixed thing rather than reverse-engineering ours. A first-party
service gets the endpoints from the template for free; a third-party service implements the spec.
Same contract, same guarantees, no shared code required — which is the whole self-contained,
plug-and-play promise, extended past our own walls.

## Fully automatic — no operator actions on the happy path

There is no "run backup" button. A schedule the operator sets ONCE (retention, frequency, per
tenant if wanted, via Phase 44's runtime surface) drives everything. Operator actions exist only
for the exceptional: trigger an out-of-band backup, or perform a restore — and a restore is the
one genuinely dangerous action, so it is capability-gated, audited write-ahead, and confirmed.

## What a backup is

Per service, per run: a sealed, checksummed archive of that service's declared collections and
object-store keys, tagged with the manifest version it was taken against. Sealed under a backup KEK
that is NOT any service's KEK — a stolen backup store yields ciphertext, and the backup key is
managed separately so restoring does not require handing out every service's own key.

## The edge cases this must survive, and how

| edge case | what must happen |
|---|---|
| process dies mid-write | a half-written backup is never marked complete; the checksum is written last, so an archive without a valid trailing checksum is ignored on restore |
| a service is down during a run | recorded as MISSED for that service, retried, and surfaced — never silently skipped, because "we backed up" while one service was absent is the exact lie this phase kills |
| clock skew across services | the backup's point-in-time is the orchestrator's clock, stamped once, not each service's |
| restore into a tenant that partly exists | import is idempotent per row identity; an existing newer row is not clobbered without an explicit `--overwrite`, so a partial restore can be re-run safely |
| storage full mid-run | the run fails loudly and the previous good backup is retained; a failed backup never deletes the last working one |
| KEK rotated since the backup | the backup records which key sealed it; a restore against a mismatched key refuses with the reason rather than producing garbage |
| a huge collection | export streams and paginates through `find`, never `scan_all` on a request path — the same discipline `safety:scans` already enforces |
| backup itself corrupted at rest | caught by the checksum at restore AND by the verification pass, not by the customer |

## Tasks

- [x] **46.1** The `backup` manifest block + `safety:backup-coverage` gate (a written collection undeclared → build red), proven to fail on a planted gap
- [x] **46.2** Template `/internal/backup/export` + `/internal/backup/import`: streamed, sealed, checksummed, idempotent import keyed on row identity
- [x] **46.3a** The PUBLIC backup contract in `docs/contracts/`: manifest `backup` block, the export
  envelope format, and the two endpoint schemas — versioned, so a third party builds against a fixed
  spec and a first-party service gets it from the template
- [x] **46.3b** Third-party registration: register a service's base URL + internal token, and it is
  discovered and scheduled like any first-party one — with a reference non-Python adapter proving the
  contract holds outside our stack
- [x] **46.3** `backup-service` core: manifest discovery, per-service orchestration, sealed+checksummed archive, retention
- [x] **46.4** The verification pass — every stored archive read BACK from the store and test-restored into a throwaway namespace by the target itself (the only party that can unseal it), compared row-for-row, then torn down. A target that cannot prove the restore is UNVERIFIED; one that restores WRONG is FAILED; only `ok AND verified` counts. Third parties implement a third endpoint, `/internal/backup/verify`, documented in the public contract
- [x] **46.5** Restore: point-in-time (from a chosen backup run), per-tenant or whole-platform, selective by service, idempotent (no-clobber unless `overwrite`), capability-gated (`platform.restore`, its own capability), echo-confirmed, audited write-ahead as CRITICAL, and never aborting the whole restore on one target. RICHER than the checklist: (a) restores ONLY archives that were verified (46.4 made load-bearing — an unproven archive cannot overwrite live data unless explicitly forced); (b) SNAPSHOTS the current state before overwriting, refusing to proceed without an undo point, so a restore is itself undoable; (c) `into_tenant` restore-as-clone for migration/sandbox/repro; (d) a `plan` preview that writes nothing; (e) `undo` that re-applies the pre-restore snapshot; (f) a full restore/undo history record
- [x] **46.6** Health, alerting, retention & schedule. A pure health evaluator answers the only question that matters — is there a recent, complete, VERIFIED backup of every service? — and flags the four ways one quietly stops being real: MISSED (a recent miss over an older good backup → degraded), STALE (no verified backup newer than the operator's tolerance → critical; catches a cron that silently stopped), UNVERIFIED (46.4 made observable — an unproven backup counts as absent), NEVER (a registered service with no verified backup → not protected). An alerter records AND pushes to a configured webhook (best-effort, never taking the check down). A retention sweep prunes to operator-set limits but NEVER a target's last verified backup. Thresholds (staleness, retention count/age) are runtime-editable via Phase 44, each read at its point of use. Crons: `health-check` hourly, `prune` daily. Surfaces: `GET /internal/backup/health` (monitor scrape), `POST /internal/backup/health/check` (evaluate+alert), `POST /internal/backup/prune`, and `GET /api/v1/operator/backup/health` (panel, platform.restore)
- [x] **46.7** Edge-case tests for all eight rows of the table above, each of the four named guards mutation-tested and PROVEN to fail on a planted defect (tests/MUTATION-46.7.md — 6 mutants, 6 killed, 0 survived). Found and fixed a real bug on the way: the orchestrator's `object_store.put` was unwrapped, so a storage-full error aborted the WHOLE run and denied every other target its backup — now caught per-target (FAILED), never aborting; a mutant reverting the fix is killed
- [x] **46.8** The restore runbook (docs/runbooks/restore-runbook.md — scope, preview, restore, verify, undo, restore-as-clone, and the guarantees table) AND the automatic restore drill: weekly (cron), for every registered service it reads its best backup back FROM THE STORE and re-verifies it against the target's throwaway-namespace test-restore — catching at-rest rot that developed since capture, on a Tuesday instead of during an outage. Records a drill history (`GET /api/v1/operator/backup/drills`) that answers "when did we last PROVE we could restore everything?", and alerts on a failed drill. Read-only against live data.

---

**Phase 46 complete.** A new service (`backup-service`, the 22nd) plus a template seam every service inherits. The organising belief — "an unverified backup is treated as no backup at all" — is enforced (46.1 coverage gate), proven at capture (46.4 verify), made observable (46.6 health), hardened against every edge case (46.7, mutation-proven), recoverable safely (46.5 restore, snapshot-first and undoable), and REHEARSED on a schedule (46.8 drill). Third parties join by three HTTP endpoints and a manifest block — same guarantees, no shared code.
