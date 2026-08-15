# deploy/seed — WhatsApp template seeding

`seed-templates.sh` registers a tenant's WhatsApp template library and starts it through Meta
review **service-to-service**, using notification-service's internal routes with `X-Internal-Token`.

## Why this exists — the workaround it replaces

WhatsApp template registration is not a user action: it belongs to the moment an establishment's
account connects, and Meta rate-limits template creation to **100/hour per WABA**, so it is a
backend job, not a browser doing hundreds of calls that stop when the tab closes.

The two write routes originally existed only behind a **user-tier JWT**. With no internal path, the
only way a script could register a library was to read identity-service's `TOKEN_PRIVATE_KEY` and
**mint a user JWT** — which means anything holding the signing key can impersonate **any** user on
the platform. This script closes that: it holds no key and mints nothing.

## Usage

```bash
NOTIFY_INTERNAL_URL=http://notification-service \
INTERNAL_SERVICE_TOKEN=***                       \
TENANT=acme                                      \
TEMPLATES_FILE=deploy/seed/templates.example.json \
  deploy/seed/seed-templates.sh

# register only; let the hourly drain cron submit to Meta:
deploy/seed/seed-templates.sh --no-submit
```

It calls, per template, `POST /internal/whatsapp/templates` (a **local upsert** — no Meta call, no
rate limit, idempotent, and it never un-approves an unchanged template), then once
`POST /internal/whatsapp/templates/submit-pending?tenant=<t>` to submit the first batch within
Meta's 100/hour cap. Anything beyond 100 is left to the **hourly drain cron** — the script does not
sleep for an hour.

## The library is yours

notification-service is content-agnostic — the template body is opaque to it. `TEMPLATES_FILE` is a
JSON array of `{ name, language, category, body, variables[], examples[] }`; see
`templates.example.json`. `variables` are the **send-time substitution NAMES** (position →
`{{1}},{{2}}`); `examples` are the **example values Meta needs at review** — a variable name like
`customer_name` is not a valid Meta example and review will reject it, so keep them separate.

## Keep the drain running

Seeding submits one batch. To finish a >100-template library and to (re)submit anything registered
later, schedule the drain — the same per-tenant cron pattern as template `sync`:

```yaml
cronJobs:
  - name: notify-wa-template-drain
    schedule: "7 * * * *"
    path: /internal/whatsapp/templates/submit-pending?tenant=<tenant>
```
