# Connect your service to erasure (DSAR) — in 3 steps

A DSAR ("delete me") is only finished when **every** system holding that person's data has answered.
compliance-service does not know what your system stores, and must never guess: completion is
**derived from acknowledgements**, never declared. So your service joins by answering for itself.

This works for **any** service, in **any** language, on **any** platform. base-services' own ten
services implement exactly this contract and are registered exactly this way — they are the
reference implementation, not a special case. Nothing in the dispatcher names them.

Contract: [`privacy-erase.schema.json`](./privacy-erase.schema.json) ·
API: [`privacy-erase.openapi.yaml`](./privacy-erase.openapi.yaml)

---

## Step 1 — implement one endpoint

Accept `{tenant, subject}` and return an ack. The path is **yours to choose** — it is configured
per target, not assumed (the default is `/internal/privacy/erase` only because that is what our
services happen to use).

```http
POST /internal/privacy/erase        # or any path you register
Authorization/X-Internal-Token: …   # however that target authenticates
{ "tenant": "t_123", "subject": "u_456", "action": "erase", "request_id": "dsar_789" }
```

```jsonc
// HTTP 200 — the work is DONE; the dispatcher records this ack for you
{
  "service": "acme-crm",          // must match your registered name
  "category": "contacts",         // matches your data-map row
  "subject_ref": "9f2a…",         // a salted PSEUDONYM, never the raw id
  "erased":   ["contacts:3", "notes:11"],
  "retained": [ { "what": "invoices:2", "basis": "legal_obligation",
                  "why": "tax law requires 10 years; buyer name pseudonymised" } ],
  "already_absent": false
}
```

**Three rules that matter more than the shape:**

1. **Be idempotent.** A DSAR retries. Erasing an already-erased subject is a no-op that still acks
   (`already_absent: true`).
2. **Never return 200 for work you did not do.** A false *"she was erased"* is the worst lie this
   system can tell. Fail loudly and be retried instead.
3. **Say what you KEPT, with its basis.** "We kept it" is always an answer with a reason attached,
   never a silence. A DSAR that cannot say why data survived has not answered the person.

### Slow erasure? Return 202 instead

If your deletion takes minutes or hours, return **`202 Accepted`** immediately and push the same ack
later:

```http
POST {compliance}/internal/compliance/dsar/{request_id}/target-ack
```

Both styles are first-class. Sync targets implement one endpoint; async targets implement one
endpoint plus one callback.

---

## Step 2 — register where you live

Discovery is **config**, never code. Add one entry (same `name=base_url` shape as `ADMIN_SERVICES`):

```bash
PRIVACY_TARGETS="acme-crm=https://crm.acme.internal,billing-service=http://billing-service:8080"
PRIVACY_ERASE_PATH="/internal/privacy/erase"          # default for all targets
PRIVACY_ERASE_PATH_ACME_CRM="/api/gdpr/forget"        # per-target override when yours differs
```

The name on the left **must** match the `service` field in your ack — acks are keyed by
`(request_id, service, category)`.

## Step 3 — declare what you hold

Register a data-map row so the DSAR knows to wait for you:

```http
POST /api/v1/compliance/data-targets   { "service": "acme-crm", "category": "contacts" }
```

This is the half that makes erasure **complete**: the DSAR fans out to exactly the targets the map
lists, and stays incomplete until each has acked. A service that holds data but is not on the map is
invisible to the DSAR — which is the failure mode this map exists to prevent.

---

## What happens then

The compliance sweeper (a CronJob) drives every **unacked** target of every in-progress DSAR:

- **200** → the ack is recorded, that target flips to acked.
- **202** → the target is left pending until it pushes its ack.
- **anything else** → retried with full-jitter backoff; after the configured max attempts the target
  is marked **FAILED and visible**, and the DSAR stays **incomplete and overdue**.

The unacked list *is* the durable work list, so the sweep is crash-safe and resumable — a restart
re-drives exactly what has not answered, and idempotency makes that harmless.

**A DSAR is never marked complete because time passed, or because a call was made.** It completes
when, and only when, every target on the map has said what it erased and what it kept.

---

## Worked example — all eighteen of ours

Every service we ship is registered through the identical config path you use for yours. There is
no internal fast path and no special case in the dispatcher — the route our own erasures travel is
the route we ask you to trust, so if it breaks for you it broke for us first.

| service | category | what it does, and why |
|---|---|---|
| identity-service | identity | **Deletes everything, retains nothing.** It owns the pseudonym→person LINKAGE, so erasing here is what orphans every `sub_…` across the platform. |
| billing-service | invoices | **Retains all, pseudonymises the buyer.** A tax invoice must outlive the request; the financial fact survives, the human does not. |
| zatca-service | tax_invoices | **Retains all, cannot even pseudonymise.** The XML is signed over its own bytes and hash-chained — editing invoice #400 invalidates #401 onward. Only the unsigned draft copy is erased. |
| file-service | user_files | **Decrements a ref-count.** One owner's erasure must never delete a blob another owner still references. |
| notification-service | messages | Deletes delivered messages and contact records. |
| analytics-service | behavioural_events | Events already carry pseudonyms; the linkage is what goes. |
| audit-service | audit_log | **Retains the chain, pseudonymises within it.** Redacting a hash-chained record breaks `verify()` from that seq onward, and tamper-evidence IS the product. |
| authz-service | access_grants | Deletes the subject's grants. |
| compliance-service | consent | **Deletes the consent record, retains the PROOF.** The proof answers "prove she consented before you processed"; deleting it destroys the defence for processing that already happened. |
| feature-flag-service | flag_overrides | Deletes per-subject overrides. |
| search-service | search_index | Deletes indexed documents for the subject. |
| webhook-service | webhook_payloads | **Redacts, keeps the row.** Delivery history answers "did it ever arrive?"; only the person leaves it. |
| job-service | job_payloads | **Redacts AND cancels.** A redacted-but-still-queued job would still contact the person we were told to forget. |
| connector-service | synced_records | **Deletes the row.** A DLQ entry gets RETRIED — a redacted one would replay a pseudonym into your CRM. |
| stream-service | recordings | **Refuses without identification.** Video is keyed by camera and time; guessing which footage holds a person would delete other people's video. |
| wallet-service | wallet_balances | **Nothing to erase** — every row is tenant-keyed. It answers anyway, so a DSAR can prove it asked. |
| tenant-service | tenant_records | Organisations, not people — except a branding contact address, which is walked. |
| api-gateway | routing | **Owns no subject data**, and says so rather than staying silent. |

**Read that column before writing your own.** Six different answers come out of the same contract —
delete, pseudonymise, retain-with-basis, redact, redact-and-cancel, refuse-without-identification —
and picking the right one is a legal judgement about YOUR data, not a coding decision. The two that
are never acceptable: reporting success for work that did not happen, and staying silent about what
you kept.

Two lessons from wiring all eighteen, which will probably apply to you too:

* **Five of our own services turned out to hold personal data nobody had catalogued**, and none of
  them had a subject-identifying column — it was inside untyped `payload` blobs and opaque
  artifacts (signed XML, video segments). No schema search would have found it. Audit by reading
  the models, not by trusting the service name.
* **An endpoint that always answers "nothing here" still earns its place.** "Never contacted" and
  "holds nothing" are indistinguishable in an incomplete system and completely different to a
  regulator — and the service that gains PII by accident later is exactly the one you assumed was
  empty.
