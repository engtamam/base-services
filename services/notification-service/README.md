# notification-service

> Self-contained, plug-and-play **FastAPI** notifications service (SaaS checklist §9).
> Email · SMS · in-app, with a **failover provider chain**, **multilingual templates**
> (injection-safe), **preferences/opt-out**, and **delivery tracking**. Backend +
> endpoints only. Built from the `_template` seed (inherits ports, auth, CWE guard,
> CI gate). **Zero new runtime deps** (stdlib `smtplib` + `urllib` + `string.Template`).

---

## Channels & providers (config-driven, `*_CHAIN` = failover order)

| Channel | Providers |
|---------|-----------|
| **Email** | `console` · `smtp` · `resend` · `sendgrid` · `mailgun` · `postmark` |
| **SMS** | `console` · `twilio` · `vonage` · **`unifonic` (KSA)** · **`taqnyat` (KSA)** · **`msegat` (KSA)** · **`mobily` (KSA)** · `webhook` (any) |
| **In-app** | built-in feed (bell / unread / mark-read) |

Set a chain to fail over, e.g. `EMAIL_CHAIN=resend,console` or `SMS_CHAIN=unifonic,console`.
The `webhook` SMS provider (`POST {to,message}` to a config URL) wires **any** other
gateway (OurSMS, 4jawaly, Yamamah…) without code. Provider APIs were verified against
each vendor's official docs.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/notify` (X-Internal-Token) | service-to-service send |
| user | `GET /api/v1/notifications` | in-app feed (unread first) |
| user | `POST /api/v1/notifications/{id}/read` | mark read |
| user | `GET/PUT /api/v1/notifications/preferences` | channel/event opt-out |
| admin | `POST/GET /api/v1/templates` (`notifications.templates` cap) | manage templates |

`POST /internal/notify` body: `{recipient, channel, template_key, locale?, variables?, user_id?, event?}`.

## Templates & preferences

- **Templates** are data (stored via `DataStore`): `key` + `locale` + `subject` + `body`,
  versioned, with **locale fallback** to `DEFAULT_LOCALE`.
- **Variables** substitute with `string.Template` (`$var`) — **substitution only, never
  evaluated** → no SSTI (CWE-94/1336). A missing variable errors (no silent leak).
- **Preferences**: a user can be globally `suppressed` or opt out per channel/event; a
  suppressed recipient is recorded `suppressed`, never sent to.
- **Delivery tracking**: every attempt records `{status: sent|failed|suppressed, provider, error}`.

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Email delivery | `EMAIL_CHAIN` | any order of the email providers |
| SMS delivery | `SMS_CHAIN` | any order of the SMS providers |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Default language | `DEFAULT_LOCALE` | any locale key |
| Feed sizing | `FEED_PAGE_SIZE` / `FEED_SCAN_LIMIT` | ints |

Provider credentials (`RESEND_API_KEY`, `TWILIO_*`, `UNIFONIC_*`, `TAQNYAT_*`, `MSEGAT_*`,
`MOBILY_*`, …) are all env — nothing hardcoded. See `.env.example`.

## Security

Injection-safe rendering (no SSTI) · recipient/channel validation · deny-by-default user
auth · `notifications.templates` capability for template admin · internal send behind a
timing-safe token · feed wrapped in the CWE respond-guard · provider hosts are fixed
trusted constants (not the SSRF surface). Send failures fail **soft** and are recorded.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

Push (FCM/APNs) · web-push · outbound signed webhooks (webhook-service) · open/click +
bounce tracking · DKIM/SPF/DMARC (DNS/infra).
