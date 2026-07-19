# Phase 38 — Wire every service to audit-service

## The finding

`audit-service` exposes a complete, token-guarded ingest endpoint that **nothing calls**.

Verified by reading the code, not inferred:

* **The endpoint exists and works.** `POST /internal/audit`
  (`services/audit-service/app/api/internal.py:59`), guarded by `X-Internal-Token` compared in
  constant time, accepting `{service, action, actor, resource, tenant, ip, metadata}` and returning
  `{id, seq, hash}` — the hash-chained, tamper-evident record that is the service's whole product.

* **0 of 17 services call it.** A repo-wide search for `AUDIT_SERVICE_URL`, `/internal/audit`, or
  any audit client found three hits, all of them prose in comments
  (`feature-flag-service/app/core/config_service.py:4`, `wallet-service/app/core/erasure.py:6`,
  `zatca-service/app/core/chain.py:3`). Zero config knobs. Zero HTTP calls.

* **No event-bus path either.** Every service has an `event_bus` port, but nothing publishes to an
  audit topic. There is no second wiring path that makes the HTTP gap harmless.

* **No client pattern to copy.** No service has `ports/audit.py` or `adapters/audit/`. The closest
  template in the repo is what Phase 37 just built: `compliance-service/app/ports/privacy_transport.py`
  with `http` and `null` adapters.

This is the **same class of defect Phase 37 existed to fix** — built, correct, and unwired — found
one layer up. Ten erasure endpoints had no callers; the audit ingest has none either. Each half was
individually correct, every test passed, and no test could see across the seam.

## Why it matters more than it looks

The user's framing is right: for a company selling to enterprises, the legal layer is not a feature,
it is the precondition for being a vendor at all. An audit log that records nothing means:

* **No answer to "who changed this?"** — the first question in any incident, dispute or investigation.
* **ISO 27001 / SOC 2 cannot be certified.** Both require evidence that privileged actions are
  logged and the log is tamper-evident. We built the tamper-evidence and then fed it nothing.
* **The DSAR story is incomplete.** Phase 37 can now prove data was erased. It cannot prove who
  requested it, who approved it, or who accessed the person's data beforehand.
* **The hash chain is worse than useless when empty.** A verifiable chain of zero events looks like
  a working control in a compliance questionnaire and provides nothing in an actual investigation.

## The rule to define (the plug-and-play question)

Phase 37's answer to "how does a service join the erasure fan-out" was: implement one endpoint,
register one config line, declare one data-map row. Audit needs the mirror of that, and the design
question is which direction the coupling runs:

* **Push (HTTP client per service)** — a service calls audit on each auditable action. Simple to
  reason about; adds a synchronous dependency on a hot path unless it is fire-and-forget, and
  fire-and-forget silently loses events, which for an audit log is the one unacceptable failure.
* **Publish (event bus)** — services publish; audit subscribes. No hot-path coupling and the bus
  already exists in every service. Delivery guarantees become the bus's problem, and a lost event
  is still a lost event.
* **Both, behind one port** — an `AuditSink` port with `http`, `eventbus` and `null` adapters,
  exactly as `PrivacyTransport` has. The service declares WHAT is auditable; config decides HOW it
  travels. `null` must refuse to look like success, and production must refuse to boot on `null` —
  the same fail-closed shape as `assert_privacy_config_ok()`.

The third is almost certainly right, and the reason is Phase 37's own lesson: the transport is a
config decision, but "which actions are auditable" is a judgement each service must declare
explicitly — because what nobody declares, nobody logs.

## Open questions to settle before building

1. **What is auditable?** Every mutation, or a declared list per service? A declared list can be
   gated (like `admin_actions` / `user_actions` already are), and a gate is the only thing that
   catches the omission — otherwise this phase recreates the same silent gap one level down.
2. **What happens when audit is down?** Fail the action, or queue locally? For a security control
   the honest default is fail-closed on privileged actions and buffered for the rest, but that is a
   product decision with real availability cost.
3. **Does the actor stay pseudonymised?** The chain stores `subject_ref` today. An audit log that
   named people would itself become a PII store subject to erasure — and erasing from a hash chain
   is what `audit-service/app/core/erasure.py` already refuses to do.
4. **What proves it stays wired?** Phase 37's answer was `safety:privacy-wiring`. This phase needs
   its equivalent — a gate comparing declared auditable actions against services actually
   configured to emit, failing in both directions.

## Proposed shape

1. `AuditSink` port + `http` / `eventbus` / `null` adapters, in `_template` first so every future
   service is born wired.
2. Per-service declared auditable actions, as a pure literal (same pattern as `admin_actions`).
3. Boot guard: production refuses a `null` sink.
4. `safety:audit-wiring` gate, mutation-tested in both directions.
5. Wire the 17 services one at a time, verifying each against its real routes.
6. End-to-end proof: a privileged action produces a chain entry that `verify()` accepts.

Ship together with Phase 37 as **v0.6.3**.
