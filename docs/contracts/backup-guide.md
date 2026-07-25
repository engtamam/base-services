# Back up your service — in 3 steps

A backup platform is only worth having if a **restore** works when you need it. Most do not, because
a collection was silently skipped or nobody ever tried restoring. backup-service closes that by
never trusting a backup it has not test-restored — and by never opening your database. It orchestrates;
your service answers for itself.

This works for **any** service, in **any** language, on **any** platform. base-services' own services
implement exactly this contract and are registered exactly this way — they are the reference
implementation, not a special case. Nothing in the orchestrator names them.

Contract: [`backup.schema.json`](./backup.schema.json)

---

## Step 1 — declare what you hold

In your `/internal/manifest`, add a `backup` object. List every dataset that must be captured to
restore you, and mark each `excluded` one that need not be — with a reason.

```json
"backup": {
  "object_prefix": "",
  "collections": [
    { "name": "invoices",  "tenant_scoped": true  },
    { "name": "plans",     "tenant_scoped": false }
  ],
  "excluded": [
    { "name": "idempo", "reason": "idempotency keys — regenerated on the next request, not data" }
  ]
}
```

**The declaration IS the coverage.** A collection you write but do not list is one a backup silently
skips, and it is missing the day you restore. There is no third state between backed-up and
excluded-with-a-reason — the third state is the hole this contract exists to prevent. (First-party
services have this enforced mechanically by the `safety:backup-coverage` gate; a third-party service
keeps its own declaration honest, because only it can see its own database.)

## Step 2 — answer two endpoints

Both are internal (service-to-service), authenticated however your service authenticates internal
callers. How you read and write your own database is entirely your business — the orchestrator only
sees the envelope.

**Export** — return a sealed, checksummed archive of your declared data:

```http
POST /internal/backup/export
X-Internal-Token: …
{ "tenant": "" }            # "" = the whole service; "t_123" = just that tenant's scoped rows
→ 200
{
  "format": "base-backup/v1",
  "service": "your-service",
  "taken_at": "2026-07-22T00:00:00Z",
  "tenant": "",
  "row_counts": { "invoices": 40, "plans": 3 },   # in the clear, so an operator sees what it holds
  "sealed": "v1:gcm:…",                            # the payload, sealed under YOUR backup key
  "checksum": "sha256:…"                           # over `sealed`, written LAST
}
```

Three rules make the archive trustworthy, and a restore must be able to rely on all three:

- **Seal the payload.** The rows never travel in the clear. `row_counts` may — they disclose no more
  than the manifest already does.
- **Checksum last.** A process that dies mid-write loses the checksum or the JSON's closing brace, so
  a truncated archive is *refused* on import rather than restored as garbage. A corrupt backup
  silently applied is worse than an obvious failure: it destroys the good data it lands on.
- **Use a backup key separate from your live secrets' key.** Restoring must not require handing out the
  key your running service uses for its own credentials.

**Import** — restore an archive you produced, idempotently:

```http
POST /internal/backup/import
X-Internal-Token: …
{ "archive": { … }, "overwrite": false }
→ 200
{ "inserted": 40, "overwritten": 0, "skipped": 3 }
```

- **Verify before writing.** Refuse a wrong format, a failed checksum, or a body your key cannot open
  — *before* anything reaches the database. A wrong key must fail loudly, not decrypt to nonsense.
- **Key on each row's own identity, and do not clobber.** A row that already exists is left alone
  unless `overwrite` is set. A restore into a tenant that partly exists must not bulldoze rows written
  after the backup, and a re-run of a half-finished restore must change nothing the second time.
- **Refuse an id-less row.** Restoring it twice would duplicate it.

## Step 3 — register with backup-service

Tell the orchestrator where you are and how to reach you — the same way billing registers a wallet
URL. From that moment you are discovered, scheduled, backed up, verified against a real restore, and
restorable, exactly like a first-party service.

```
POST /internal/backup/targets
{ "service": "your-service", "base_url": "https://your-service.internal", "token_ref": "…" }
```

You write two endpoints once. You never write a backup system.

---

## Why it is shaped this way

- **The orchestrator never opens your database.** It cannot understand twenty services' schemas, and
  it should not try — that is what makes this plug-and-play rather than a fragile central importer.
  You expose what only you understand.
- **An unverified backup counts as nothing.** backup-service test-restores every archive into a
  throwaway space and compares it. A backup that has not been proven to restore does not silence an
  alert, because "we have backups" is precisely the sentence that is false at the worst moment.
- **Discovery is your manifest.** A holder nobody declared is one nobody backs up — the same rule the
  DSAR fan-out uses to find who holds a person. Declaring is not optional paperwork; it is the
  coverage.
