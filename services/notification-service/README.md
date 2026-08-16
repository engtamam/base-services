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
| internal | `POST /internal/notifications/drain` | worker tick: re-attempt queued deliveries |
| user | `GET /api/v1/notifications` | in-app feed (unread first) |
| user | `POST /api/v1/notifications/{id}/read` | mark read |
| user | `GET/PUT /api/v1/notifications/preferences` | channel/event opt-out |
| admin | `POST/GET /api/v1/templates` (`notifications.templates` cap) | manage templates |

`POST /internal/notify` body: `{recipient, channel, template_key, locale?, variables?, user_id?, event?, idempotency_key?}`.

> **WhatsApp template library — a backend job, not a browser.** Registering a shop's ~66 texts × 2
> languages belongs to the moment its account connects, and Meta rate-limits template creation to
> **100/hour per WABA**. So the two user-tier write routes have **internal (`X-Internal-Token`) twins**
> with the tenant in the body — `POST /internal/whatsapp/templates` (register, same approved-guard: a
> no-op re-register never un-approves) and `POST /internal/whatsapp/templates/{name}/{language}/submit`.
> Register the library once (a local upsert, no Meta call), then **cron
> `POST /internal/whatsapp/templates/submit-pending?tenant=<t>` hourly**: it submits the next batch of
> never-submitted templates within Meta's cap (`WHATSAPP_TEMPLATE_SUBMIT_LIMIT`, ≤100), oldest-first,
> idempotent, until the library is through — the throttle lives here, so no caller reimplements it.

> **A connected number cannot send until it is REGISTERED** (Meta error 133010 otherwise). Registration
> is **per phone number** — a WABA holds many (a number per branch for a chain) — and needs the
> business's 6-digit two-step PIN. Three ways in:
> - **At connect:** pass `pin` to Embedded Signup and `exchange()` registers the number as a fourth
>   step, reported (`registered`, `register_reason`) — a mute "connected" number never happens.
> - **Standalone:** `POST /internal/whatsapp/register {tenant, phone_number_id?, pin?}` — the code
>   lives ~30s and can't be replayed, so this recovers a number whose signup registration failed, and
>   re-registers after account events. The PIN is **sealed per number** on success, so `pin` may be
>   omitted on a re-registration. Outcomes: `registered:true`, or `{registered:false, reason}` where
>   reason is `WRONG_PIN` · `RATE_LIMITED` (Meta's 10-per-72h lock, 133016 — *wait*) · `UNREACHABLE` ·
>   `NOT_CONNECTED` · `NO_PIN`. A malformed PIN is refused (422) before it spends one of the 10 attempts.
> - **Readiness:** `GET /internal/whatsapp/status?tenant=&phone_number_id=` reads a number's real state
>   from Meta — `platform_type` (native CLOUD_API vs a coexistence number, which stops after ~14 days
>   of primary-device inactivity), `code_verification_status`, `throughput`, `quality_rating`.
>
> The PIN is sealed beside the token (`WHATSAPP_2FA_PIN_<phone_id>`) and **never returned by any read**.

> **Inbound Meta webhook — the platform owns it** (it holds the app secret needed to verify). `GET
> /webhooks/whatsapp` answers the subscription handshake (`hub.challenge` iff `hub.verify_token`
> matches `WHATSAPP_WEBHOOK_VERIFY_TOKEN`, constant-time); `POST /webhooks/whatsapp` verifies
> `X-Hub-Signature-256` (HMAC-SHA256 over the raw body under `META_APP_SECRET`, constant-time,
> fail-closed) then **fans every change out to the event bus under `whatsapp.<field>`** — delivery
> statuses (also exploded to `whatsapp.status`), inbound messages, `account_update`, `user_preferences`,
> and **any unrecognised future field, never filtered**. A consumer subscribes to what it needs; an
> unknown type is emitted rather than dropped, so this can't silently recreate the gap it closes.

> **An outage delays a message, it does not lose it.** `NOTIFY_DELIVERY_MODE=queued` (default): a
> failed send is classified (retryable vs permanent), backed off with full jitter, retried by the
> drain worker, and **dead-lettered** after `NOTIFY_MAX_ATTEMPTS` (distinct from `failed`). Point a
> CronJob at `/internal/notifications/drain`. The `idempotency_key` dedupes retries — including
> while a message is still queued — so she never gets two.

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

## Rate limiting (every request, config-driven)

Sliding-window (GCRA) limiter — **no boundary burst**, and **atomic** (a check-and-increment race
cannot exceed the limit). Every response carries `RateLimit-Limit/Remaining/Reset`; a 429 also
carries `Retry-After`. On a Redis outage it **degrades to a per-pod limiter** (coarser, still
limiting) rather than failing open or 500-ing.

| Choice | Env knob | Options / example |
|--------|----------|-------------------|
| Backend | `RATELIMITER_DRIVER` | `memory`(per-pod) · `redis`(shared) |
| Budget | `RATELIMIT_MAX` / `RATELIMIT_WINDOW_SECONDS` | e.g. `100` / `60` |
| Key | `RATELIMIT_KEY_PARTS` | `subject,ip` · add `tenant`/`route` |
| Per-route budgets | `RATELIMIT_ROUTES` | `[{"prefix","class","limit","window_s","cost"}]` |
| Only count failures | `RATELIMIT_COUNT_STATUSES` | e.g. `401` (a 403 will not lock a user out) |
| On Redis outage | `RATELIMIT_DEGRADE` | `local`(default) · `closed` · `open` |

> The subject is resolved **inside** the limiter (its own optional dependency), not read from
> pre-set request state — so an authenticated request keys per-subject regardless of the route's
> dependency order. No "one office NAT shares a bucket" surprise. Enforced by `safety:edge`.

Operator unblock (never `FLUSHALL`): `POST /internal/ratelimit/unblock {"key":"<key>"}`.

> The public edge (Caddy) is generated from `deploy/edge/edge.json` by `tools/edge/render.py` —
> one config source, injection-refused, HSTS only under TLS, non-root container. `nginx` is a
> future renderer of the same shape.
