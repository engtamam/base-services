# Phase 54 — notification-service: WhatsApp channel + approved-template lifecycle

A fourth notification channel, `whatsapp`, added the SAME config-driven way as email and SMS: a
provider CHAIN an operator reorders at runtime, a port every driver implements, and console/webhook
drivers for dev. The one thing WhatsApp needs that the others do not: **business-initiated messages
must use a template Meta has APPROVED**, so the channel carries a template lifecycle — register,
submit to Meta, track status, and substitute variables at send.

Generic to the core: this service stores no template *content* semantics and no domain logic. A
template is an opaque `{name, language, category, body, variables}` the adopter owns; the service
manages its lifecycle and substitutes variables, nothing more.

## What already exists (so we mirror it, not fight it)

- **Channels are failover chains.** `EMAIL_CHAIN` / `SMS_CHAIN` are comma lists an operator reorders
  at runtime (`RUNTIME_POLICY`, resolved per tenant in `provider_pool.py`). `FailoverEmail` /
  `FailoverSms` try each provider until one wins; `console` is the floor so a chain is never empty.
- **Ports + adapters.** `ports/sms_provider.py` is one `send`; `adapters/sms/*` are the drivers.
- **`CHANNELS`** in `core/notifier.py` is the allow-list a send is dispatched against; preferences
  can opt a recipient out of a channel.

## The work

### 54.1 — the WhatsApp channel (port, adapters, chain) — mirror email/SMS

- `ports/whatsapp_provider.py`: `send(to, template_name, language, variables) -> bool`. WhatsApp is
  template-shaped, not free-text, so the signature carries the approved template + its variables.
- `adapters/whatsapp/{meta,console,webhook}.py`: **meta** = WhatsApp Cloud API (Graph
  `/{phone_number_id}/messages`, template message); **console** prints (dev); **webhook** POSTs to a
  configurable URL (dev/QR gateways). Meta credentials (`WHATSAPP_ACCESS_TOKEN`,
  `WHATSAPP_PHONE_NUMBER_ID`) resolved per tenant like every other provider credential.
- `WHATSAPP_CHAIN` config + `whatsapp_chain` derived list + `RUNTIME_POLICY` entry; `FailoverWhatsApp`
  in `failover.py`; `_build_chain` + `provider_pool` `build_whatsapp`; `CHANNELS += "whatsapp"`.

### 54.2 — the approved-template registry (generic, content-agnostic)

- A `whatsapp_templates` collection: `{tenant, name, language, category, body, variables[], status,
  provider_template_id, rejection_reason, created, updated}`. `category` ∈
  `UTILITY | MARKETING | AUTHENTICATION` (Meta's set), validated; `variables` is the ordered list the
  body references (`{{1}}`, `{{2}}` or named). Status starts `pending`.
- `core/whatsapp_templates.py`: `register` (write pending), `list`, `get`. Tenant-scoped, filtered
  reads only. The service never inspects `body` beyond counting/ordering variables for substitution.

### 54.3 — submit to Meta + status tracking

- A `WhatsAppTemplateManager` port with a Meta adapter (Graph `POST /{waba_id}/message_templates`,
  `GET` status) and console/webhook dev stubs. `submit(template)` sends it and records
  `provider_template_id` + `pending`; `sync_status` polls Meta and moves rows to `approved` /
  `rejected` (+ `rejection_reason`). Driven by an internal `/internal/whatsapp/templates/sync` route
  on the platform scheduler — a status change is a durable fact, never a bus hint.

### 54.4 — auto-substitute variables at send

- Sending on `whatsapp` resolves the APPROVED template for `(tenant, name, language)`. Not approved
  (pending/rejected/absent) ⇒ the send is refused with a clear reason, never silently dropped — an
  unapproved WhatsApp template cannot be delivered, so pretending otherwise hides a real block.
- Variables are substituted positionally/by-name from the caller's `variables` map into the provider
  `send`. Count mismatch (fewer/more than the template declares) is refused, not guessed.

### 54.5 — operator surface + wiring

- Routes: register a template, submit it, list/status (operator, `notify.manage`); the existing send
  path accepts `channel=whatsapp` with `template_name` + `variables`. Admin + auditable actions
  declared; manifest capabilities/endpoints/events updated.

### 54.6 — tests + CI discipline

- Mirror the email/SMS provider and failover tests: chain failover, per-tenant credential resolution
  and runtime chain switch (the `provider_pool` tests), console/webhook/meta drivers. Template
  lifecycle tests: register → pending, submit → id, sync → approved/rejected, send-before-approved
  refused, variable-count-mismatch refused, substitution correct. All standard gates + full suite.

## Definition of Done

- `whatsapp` is a first-class channel: `WHATSAPP_CHAIN` (runtime, per-tenant), port + meta/console/
  webhook adapters, failover, `provider_pool` resolution, `CHANNELS` includes it, opt-out honoured.
- Template lifecycle: register (pending) → submit (Meta id) → sync (approved/rejected + reason);
  tenant-scoped; the service holds no template semantics beyond variables.
- Send on whatsapp uses an APPROVED template only, substitutes variables, refuses on
  not-approved / count-mismatch with a stated reason; never drops silently.
- Meta credentials resolved per tenant and never returned by any route; egress host allow-listed.
- Same ports/adapters shape, tests, and gates as email/SMS; full suite + security gate green.

## Tasks

- [ ] **54.1** WhatsApp channel: `whatsapp_provider` port; meta/console/webhook adapters;
  `WHATSAPP_CHAIN` + `RUNTIME_POLICY`; `FailoverWhatsApp`; container + `provider_pool` wiring;
  `CHANNELS += whatsapp`; per-tenant Meta credentials.
- [ ] **54.2** Template registry: `whatsapp_templates` store + `core/whatsapp_templates.py`
  (register/list/get), category + variables validated, tenant-scoped filtered reads.
- [ ] **54.3** Submit + status: `WhatsAppTemplateManager` port + Meta adapter (+ console/webhook
  stubs); `submit` / `sync_status`; `/internal/whatsapp/templates/sync` route.
- [ ] **54.4** Send-time substitution: resolve approved template, refuse not-approved /
  count-mismatch, substitute variables into the provider send.
- [ ] **54.5** Operator routes (register/submit/list/status) + send accepts whatsapp; admin +
  auditable actions; manifest capabilities/endpoints/events.
- [ ] **54.6** Tests mirroring email/SMS (failover, per-tenant creds, runtime switch) + template
  lifecycle tests; all gates + security green.
