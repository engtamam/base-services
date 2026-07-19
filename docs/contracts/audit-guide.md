# Connect your service to the audit log

Every privileged action on this platform lands in a hash-chained log: each entry seals the previous
one, so a later edit breaks `verify()` from that point onward. Tamper-evidence is the point.

Your service joins it the same way ours do — no internal fast path, so if it breaks for you it
broke for us first.

> **The instructions live in the code, not here.**
> `services/_template/app/auditable_actions.py` is the guide: copy it, read the header, fill in the
> list. It explains what belongs in the list, the four sources people forget, and how to decide
> `critical` — at the moment you are making those decisions, instead of in a document you would
> have to remember to open. This page covers only what that file cannot: the wire contract, the
> config, and what each of our services chose.

---

## The wire contract

`POST {AUDIT_SERVICE_URL}{AUDIT_INGEST_PATH}` with `X-Internal-Token`:

```json
{ "service": "billing-service", "action": "invoice.voided", "actor": "sub_a1b2…",
  "resource": "invoice:inv_7", "tenant": "acme", "ip": "10.0.0.1", "metadata": {} }
```

Returns `{"id", "seq", "hash"}` — the position and seal that prove the event is in the chain. A
`200` **without** a hash is treated as a failure: the seq and hash *are* the evidence, and
accepting a bare 200 is how a chain ends up trusted while empty.

`actor` is a **pseudonym** (`subject_ref`), never a name or email. An audit log holding raw
identities becomes a PII store that cannot honour an erasure — deleting a row from a hash chain is
exactly what breaks it.

## Config

```bash
AUDIT_SINK_DRIVER=http                       # null | http | eventbus
AUDIT_SERVICE_URL=http://audit-service:8000
AUDIT_INGEST_PATH=/internal/audit            # OUR convention — override for your own endpoint
AUDIT_FAIL_CLOSED=true
```

| driver | what you get |
|---|---|
| `http` | the receipt carries **seq and hash** — the event is *in* the chain, sealed and positioned. Costs a round trip on the action's path |
| `eventbus` | no hot-path cost, and audit being down blocks nothing. But it acknowledges **publication, not recording**: no seq, no hash, and if the consumer never runs the chain stays empty |
| `null` | records nothing and **says so**. Production refuses to boot on it |

Production also refuses `http` with no URL. A service that cannot witness its own privileged
actions must not serve traffic as though it can.

## Emitting

```python
await request.app.state.auditor.record(
    "permission.granted",
    actor=subject_ref, resource=f"grant:{grant.id}",
    tenant=principal.tenant, ip=request.client.host,
    metadata={"capability": cap},
)
```

For a `critical` action, let `AuditUnavailable` propagate — the API layer turns it into a 503. That
is the fail-closed decision doing its job.

## What is checked for you

- **At boot** — declarations are validated (dotted names, known source, explicit boolean
  criticality, a `why` on every critical action). A malformed one stops the service rather than
  surfacing months later as a hole in the log.
- **At emit** — an undeclared action raises. An event emitted but never declared is invisible to
  the gate *and* to the investigator searching the log: audited in name only.
- **In CI** — `safety:audit-wiring` fails if a service declares actions but is not configured to
  emit, or is configured to emit but declares nothing.

---

## Two things we got wrong, so you do not have to

**The log was empty for months and nothing failed.** A complete, token-guarded ingest endpoint with
no callers. Every test passed and the chain verified perfectly — because a missing event leaves no
hole. An audit log cannot tell you what it never received. That is why declarations are explicit
and gated rather than inferred, and why the boot guard is fail-closed.

**We do not retry a failed emit.** The ingest contract has no idempotency key, so a retry after a
timeout is indistinguishable from a second action and would append a *duplicate* to a tamper-evident
log. A log that invents actions is no more trustworthy than one that loses them, and it is harder
to spot. If you add retries, add an idempotency key to the contract first.
