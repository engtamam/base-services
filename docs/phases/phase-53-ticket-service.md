# Phase 53 — ticket-service: a general typed-thread engine, feedback first

A new self-contained service to receive, track and resolve any kind of request or note — a customer
reporting a problem, an academy leaving feedback on a template, a billing question. Each item is a
**ticket**. The first use is collecting feedback on templates, but the ask is explicit: build the
GENERAL service, feedback is just the first ticket *type*.

## The belief this phase is built to destroy

> "A ticketing service models support tickets. New kinds of ticket (feedback, billing…) and new
> statuses are code changes, and it emails people when something happens."

Every clause is what makes a ticketing service un-reusable. This one is built the opposite way:

- A ticket is a **typed thread over a state machine that is DATA, not code**. The type, its
  statuses, AND the allowed transitions between them are config. Adding a ticket type later is a
  setting, not a project — the load-bearing requirement of the whole ask.
- It never sends mail. It emits domain facts and hands delivery to notification-service.
- It holds PII, so it is erasable from day one — a service that cannot answer a DSAR is a
  compliance hole the moment it stores its first message.

## The shape

- **Ticket** = `{id, tenant, type, subject, status, raiser_ref, assignee, priority, ref, tags,
  created, updated}`. `raiser_ref` is a pseudonym (HMAC over tenant+subject, PII_SALT) — provable
  AND erasable, the same pattern compliance/audit already use.
- **`ref`** — a polymorphic pointer `{kind, id}` (`document:…`, `invoice:…`, `plan:…`): the "optional
  pointer to whatever it refers to". Opaque to this service; meaningful to whoever raised it.
- **History is the truth.** An append-only `ticket_events` log (`created`, `replied`,
  `status_changed`, `assigned`, `tagged`, `rated`, `ai_suggested`). The current status is a cached
  projection of the log, never the other way round — so "who did what and when" is a read, not a
  reconstruction. A reply is an event; there is no separate mutable "replies" table to drift.

## The work

### 53.1 — scaffold the service (the 23rd), zero-infra defaults

- `scripts/create_service.py ticket-service` — the standard skeleton (ports/adapters, manifest,
  config, admin/user/auditable actions, runtime_policy, backup_manifest, observability, security
  middleware). Memory datastore default; wire to real infra by config only.
- Capabilities: `tickets.self` (raise / reply / view OWN), `tickets.manage` + `platform.tickets`
  (operator: triage, assign, transition, search across the tenant). Advertised in the manifest.

### 53.2 — the type + workflow config, validated at boot (THE core)

- Ticket **types** live in `config/product.json` (per-tenant-overridable via runtime_config later),
  each: `{key, label{en,ar}, statuses[], transitions{from:[to…]}, terminal[], required_fields[],
  reply_capability, transition_capabilities{status:cap}, notify{on[],template_prefix}}`.
- A boot guard REJECTS an incoherent type: a transition to an undeclared status, no terminal state,
  a start status nothing reaches. An inert or malformed workflow must fail at boot, not at the first
  ticket — the advertised-knobs discipline applied to the state machine.
- Feedback is the first declared type; support/billing ship as commented examples so adding one is
  copy-a-block. A declared-contract CI gate reads the types as a pure literal and checks coherence.

### 53.3 — the ticket engine + the state machine as an enforced invariant

- `TicketService`: `open`, `reply`, `transition`, `assign`, `tag`, `rate`, each appending to the
  event log and updating the projection. `transition(ticket, to, actor)` REFUSES a move the type's
  `transitions` does not allow (409 `INVALID_TRANSITION`) and refuses an actor lacking the
  transition's capability (403) — the workflow is enforced server-side, not trusted from the client.
- `required_fields` enforced at `open` per type (feedback requires `ref`). Reopen is just a declared
  transition out of a non-terminal resolved state; terminal states accept no transition.

### 53.4 — multi-customer safety + operator search/filter

- Tenant from the token, never the body. Customer scope = `raiser_ref == mine`; operator scope =
  tenant-wide. Every list is a FILTERED `find()` with keyset pagination — never a scan
  (`safety:scans`). Filters: type, status, assignee, ref, tag, date range.
- Optional search-service indexing driver: wired ⇒ full-text over subject/body; absent ⇒ datastore
  filters. Plug-and-play — a provider that cannot index SAYS so, it does not fake it.

### 53.5 — notification via the existing service, durably (never sends mail itself)

- On `created` / `replied` / `status_changed`, emit a `ticket.*` domain event AND record a durable
  **notifier outbox** row that POSTs to notification-service (mirror billing's grant outbox). A
  "notify people" promise must survive a dropped at-most-once bus message. Templates + recipients
  come from the type's `notify` config; the service composes no message bodies of its own.

### 53.6 — erasable from day one (the compliance contract)

- Implement the internal `erase_subject` contract and declare `erasure_category: "subject"` in the
  manifest, so the DSAR fan-out (`/compliance/me/dsar`) actually reaches tickets. Erasure
  pseudonymises the raiser and redacts message/subject bodies while KEEPING the ticket's shape and
  history (a resolved dispute must remain provable) — retain-with-reason, never a silent drop.

### 53.7 — the AI advisory layer (assistant-service), human-in-the-loop, OFF by default

- A `SuggestionProvider` port (null driver default) behind which assistant-service can: auto-triage
  a new ticket (suggest type/priority/assignee above a confidence knob, else just suggest), draft a
  reply an agent edits and sends, summarise a long thread, and cluster template-feedback into themes
  + sentiment (the first use case's real payoff).
- NON-NEGOTIABLE: advisory only. AI never transitions, closes, or sends on its own; every
  suggestion is an `ai_suggested` event recorded beside what the human then did. Content sent to the
  model is tenant-scoped and pseudonymised/redacted; consent respected. The map-not-authority rule
  the assistant already follows.

## Definition of Done

- A new `ticket-service` with `tickets.self` / `tickets.manage` capabilities, memory-default,
  passing the standard gates (manifest-routes, openapi, admin/user actions, audit wiring,
  dead-code, safety:scans, security-gate, backup coverage).
- Ticket types + their full state machine (statuses, transitions, terminal, per-transition caps,
  required fields, notify) are config; a boot guard + CI gate reject an incoherent type; adding a
  type is a config entry with no logic change.
- The state machine is ENFORCED: an illegal transition is 409, an under-capability transition 403,
  a missing required field 422 — proven by mutation tests.
- Multi-tenant: customer sees only their own, operator sees the tenant; every list is a filtered
  find; a cross-tenant read is structurally impossible.
- Notifications go through notification-service via a durable outbox; the service sends no mail and
  composes no bodies. Search works with the driver wired and degrades to filters without it.
- Erasure: `erase_subject` pseudonymises the raiser and redacts bodies while retaining shape +
  history with a stated basis; the manifest declares the erasure category.
- AI is advisory, off by default, never in the authority path; suggestions are logged; content is
  tenant-scoped and redacted.

## Tasks

- [x] **53.1** Scaffold `ticket-service` (create_service.py); capabilities `tickets.self` /
  `tickets.manage` / `platform.tickets`; zero-infra defaults; standard gates green.
- [x] **53.2** Type + workflow config in product.json (statuses/transitions/terminal/required/
  per-transition caps/notify); boot guard + CI gate reject an incoherent type; feedback first.
- [x] **53.3** `TicketService` engine over an append-only event log; state machine ENFORCED
  (illegal transition 409, under-cap 403, required field 422); reopen + terminal handled.
- [x] **53.4** Multi-tenant scope + operator search/filter — filtered find + keyset pages (no scan);
  optional search-service indexing driver with filter fallback.
- [x] **53.5** Durable notifier outbox → notification-service on created/replied/status_changed;
  templates from type config; the service never sends mail itself.
- [x] **53.6** `erase_subject` internal contract + manifest `erasure_category`; pseudonymise raiser,
  redact bodies, retain shape/history with a stated basis.
- [x] **53.7** `SuggestionProvider` port (null default) for assistant-service: triage / draft /
  summarise / feedback-clustering; advisory only, logged as `ai_suggested`, redacted, human-confirmed.
- [x] **53.8** Admin + user actions catalogued (raise/reply/view own for the user; triage/assign/
  transition/search/AI-suggest for the operator); auditable actions for every state change.


---

**Phase 53 complete.** A general ticket-service (the 23rd): a ticket is a typed thread over a state machine that is DATA — types, statuses, transitions, per-transition capabilities and notify are all config, so a new type is a setting, not a project. History is an append-only event log (status is a projection, ordered by a strict seq). Multi-tenant (customer sees own, operator the tenant; filtered finds, no scan). Never sends mail — a durable notifier outbox drains to notification-service. Erasable from day one (redact free text, keep the pseudonymous record). Advisory AI via assistant-service (triage + draft reply), logged as ai_suggested, off by default, never in the authority path. 358 tests; all gates + bandit + pip-audit clean. feedback is the first type.
