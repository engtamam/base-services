# Restore runbook

The one procedure you hope never to run, written for the day you have to. Read it now, not then.

Backups here are automatic, verified at capture (46.4), watched for staleness (46.6), and
**rehearsed on a schedule** (the drill, below). A restore is the one action that overwrites live
data, so it is deliberate: capability-gated, echo-confirmed, snapshot-first, audited, and undoable.

---

## 0. Before anything — is this actually a restore?

A restore overwrites live data. It is the right tool for **data loss or corruption** (a bad
migration, a bulk delete, ransomware), not for an outage where the data is intact. If the data is
fine and a service is merely down, this is the wrong page — restart the service.

If you are restoring, decide the **scope** first:

- **One tenant** — a customer's data was corrupted. `tenant=<id>`.
- **One service** — one service's store was lost. `services=["billing-service"]`.
- **Whole platform** — a catastrophe. No `tenant`, no `services` filter.

---

## 1. Find the backup to restore from

```
GET /api/v1/operator/backup/health          # is every service protected right now?
GET /internal/backup/runs                   # the run history — pick a run_id
```

Pick the **most recent run that is `complete`** (every target verified) unless you are restoring to a
specific point in time before an incident. Note its `run_id`.

## 2. PREVIEW — this writes nothing

```
POST /api/v1/operator/restore/plan
{ "run_id": "run_…", "tenant": "", "services": [] }
```

Read the preview. Every target should be `eligible: true`. A `blocked_reason` of *"never verified"*
means that service's backup in this run was never proven restorable — **do not force past it** unless
you understand why. This is the screen you confirm reality against.

## 3. RESTORE

```
POST /api/v1/operator/restore
{
  "run_id":  "run_…",
  "confirm": "run_…",          # echo the run_id — a mis-click cannot trigger a restore
  "tenant":  "",               # or a tenant id for a per-tenant restore
  "services": [],              # or a subset
  "overwrite": false,          # false LEAVES newer rows alone; true bulldozes to the backup state
  "require_verified": true,    # keep true — never restore an unproven backup
  "safety_snapshot": true      # keep true — captures current state first, so this is undoable
}
```

- **`overwrite: false`** (default) is safe re-runnable: it inserts what is missing and leaves rows
  written after the backup alone. Use it to fill a gap. Use `true` only to force the store back to
  exactly the backup's state, knowingly discarding anything newer.
- **`safety_snapshot: true`** (default) captures each target's current state *before* overwriting.
  If a target's snapshot cannot be taken, that target is **not** restored — you are never left
  without an undo point. Do not waive this unless you have your own out-of-band snapshot.

The response records, per target: `restored` / `skipped` / `failed`, the row counts, and the
`snapshot_key` used for undo. Note the returned `id` — you need it to undo.

## 4. VERIFY the restore

```
GET /internal/backup/health                 # every restored service should be healthy
```

Spot-check the restored service's own data through its normal API. A restore is not done until you
have looked at the data, not just the status code.

## 5. If it went wrong — UNDO

```
POST /api/v1/operator/restore/{restore_id}/undo
```

Re-applies each target's pre-restore snapshot (overwrite forced) — puts back exactly what was there
before the restore. This is why `safety_snapshot` must stay on.

---

## Restore-as-clone (migration / sandbox / support repro)

To stand a customer's data up under a **different** tenant instead of restoring in place:

```
POST /api/v1/operator/restore
{ "run_id": "run_…", "confirm": "run_…", "tenant": "src", "into_tenant": "dst", … }
```

Only tenant-carrying rows are reassigned; global datasets are left alone; row ids are preserved so
`overwrite: false` still guards whatever already exists under `dst`.

---

## The drill — why you can trust step 3 works

Every week the platform runs a **restore drill** (`POST /internal/backup/drill`, cron): for every
service it reads its best backup back **from the store** and asks the service to test-restore it into
a throwaway namespace and compare — the same safe path as capture-time verification, but run again
weeks later against the at-rest bytes. A backup that verified when taken but rotted since is caught
here, on a Tuesday, instead of by a customer during an outage.

```
GET /api/v1/operator/backup/drills          # when did we last PROVE we could restore everything?
```

If the latest drill is `passed`, step 3 is not a leap of faith — it is a rehearsed procedure. If it
is `failed`, treat that service as **not restorable** and fix the backup before you need it.

---

## Preconditions & guarantees (reference)

| control | what it guarantees |
|---|---|
| `platform.restore` capability | only a DR operator can restore — never folded into config/secrets |
| echo-to-confirm | a replayed request or mis-click cannot trigger a restore |
| audited write-ahead, CRITICAL | a restore that cannot be recorded does not happen |
| verified-only (default) | you never overwrite live data from an unproven backup |
| safety snapshot (default) | every restore is undoable; no snapshot ⇒ no overwrite |
| never-abort | one target failing does not deny the others their restore |
| separate backup KEK | restoring does not require handing out any live service's key |
