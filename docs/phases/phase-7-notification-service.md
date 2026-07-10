# Phase 7 — notification-service (detailed steps)

> **Goal:** the second Tier-1 service — **notification-service** — mapping to SaaS
> checklist §9. Smart & plug-and-play: **channel providers behind ports** (email,
> sms) with a **failover chain**, **templates as data** (multilingual, safe — no
> SSTI), **preferences honored**, **delivery tracked**, and an **in-app feed** —
> all config-driven, **zero new runtime deps** (stdlib `smtplib` + `string.Template`),
> zero-infra by default (console providers).
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §9._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive-only · CWE-guard · backend-only._

---

## 0. Definition of done (Phase 7)

- [ ] `services/notification-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gates).
- [ ] **Pluggable channels by config:** `EmailProvider` (console/smtp), `SmsProvider` (console/http), each behind a **failover chain**; template rendering via `TemplateRenderer`. All storage via the existing `DataStore` port.
- [ ] **Templates as data** — versioned, **multilingual** (locale), variable substitution that is **injection-safe** (`string.Template`, no code/logic execution → no SSTI, CWE-94/1336).
- [ ] **Preferences** honored — per-user channel/event opt-in + a suppression (opt-out) list; a suppressed/disabled recipient is **not** sent to.
- [ ] **Delivery tracking** — every send records a delivery row (queued/sent/failed + provider + error).
- [ ] **In-app feed** — store, list (unread first), mark-read.
- [ ] Endpoints: internal `/internal/notify` (service-to-service send), user `/api/v1/notifications` (feed, read, preferences), admin template CRUD (cap-gated).
- [ ] **Security:** recipient validation, **no template injection**, egress via **config allowlist** (SSRF, CWE-918) for http providers, no secret leak, rate-limited.
- [ ] **Zero new deps**; `pytest` + `ci/security_gate.py` green; live boot proven (send → delivery recorded; in-app feed).

---

## 1. Prerequisites & decisions

1. **Zero new runtime deps.** Email uses stdlib `smtplib`/`email`; SMS "http" provider uses stdlib `urllib` against a **config-allow-listed** endpoint; templating uses stdlib `string.Template` (safe: `$var` substitution only, no expressions → **no SSTI**). Console providers (default) just record — zero infra.
2. **Failover is first-class.** A channel resolves to a **chain** of providers (e.g. `smtp,console`); send tries each until one succeeds, recording attempts.
3. **All state via `DataStore`.** Templates, preferences, deliveries, in-app items are collections — memory today, postgres by config.
4. **Sending is service-to-service.** Other services POST `/internal/notify`; users only read their feed/preferences. (Push, web-push, and outbound webhooks are deferred — webhook-service owns signed webhooks.)

---

## 2. Steps

### Step 7.1 — Generate skeleton
- `python scripts/create_service.py notification-service --desc "Notifications: email, sms, in-app"`.
- No new deps → verification reuses the template venv (fast). Confirm inherited tests + gate green.
- **Check:** skeleton verified green.

### Step 7.2 — Config (zero-hardcoded, zero-infra defaults)
- Add knobs: `EMAIL_CHAIN` (default `console`), `SMS_CHAIN` (default `console`), SMTP settings (`SMTP_HOST/PORT/USER/PASSWORD/FROM/STARTTLS`), `SMS_HTTP_URL` (+ egress allow via existing `EGRESS_ALLOWED_HOSTS`), `DEFAULT_LOCALE` (`en`). Update `.env.example`.
- **Check:** boots with console defaults; fail-fast on malformed values.

### Step 7.3 — Ports
- `app/ports/email_provider.py` (`send(to, subject, html, text, sender) -> bool`), `app/ports/sms_provider.py` (`send(to, message) -> bool`), `app/ports/template_renderer.py` (`render(subject_tmpl, body_tmpl, vars) -> (subject, body)`).
- **Check:** import pulls no third-party driver.

### Step 7.4 — Adapters + failover + registry
- Email: `console.py` (records), `smtp.py` (stdlib). SMS: `console.py`, `http.py` (urllib, **egress-allowlisted**). Renderer: `safe.py` (`string.Template`, missing var → error, never eval).
- `FailoverChannel` wraps an ordered provider list; `build_chain(spec, factory)` from the `*_CHAIN` config (allowlist per provider name; unknown → ConfigError).
- Extend `container.py`: `email`, `sms`, `renderer` fields.
- **Check:** unknown provider → ConfigError; console chain builds; failover falls through on failure.

### Step 7.5 — Domain: templates, preferences, deliveries, in-app
- `core/models.py`: `Template` (key, locale, version, subject, body), `Preference` (user, channel/event opt-ins, suppressed), `Delivery` (id, recipient, channel, template, status, provider, error, ts), `InAppItem` (id, user, title, body, read, ts).
- Repos over `DataStore`: `TemplateStore` (by key+locale, latest version), `PreferenceStore`, `DeliveryStore`, `InAppStore` (list unread-first, mark-read).
- **Check:** CRUD + locale fallback (missing locale → `DEFAULT_LOCALE`).

### Step 7.6 — Core NotificationService
- `core/notifier.py`: `notify(recipient, channel, template_key, locale, vars, user_id?)` →
  resolve preference/suppression (skip if opted out) → load template (locale fallback) → **render** →
  send via the channel's **failover chain** → **record delivery** → (in-app channel also stores an `InAppItem`).
- In-app helpers: `feed(user)`, `mark_read(user, id)`.
- **Check:** unit tests: opt-out suppression, locale fallback, failover success/all-fail, delivery recorded.

### Step 7.7 — Routes
- Internal: `POST /internal/notify` (X-Internal-Token) — service-to-service send.
- User: `GET /api/v1/notifications` (my feed), `POST /api/v1/notifications/{id}/read`, `GET/PUT /api/v1/notifications/preferences` (all `require_user`, tenant/subject-scoped).
- Admin: `POST/GET /api/v1/templates` (`require_capability("notifications.templates")`).
- Wire `rate_limit`, deny-by-default, `guard.step("respond")` on feed (no secret leak), `guard.step("egress")` around http provider calls (SSRF).
- **Check:** routes mount; deny-by-default holds.

### Step 7.8 — Manifest & wiring
- Manifest capabilities + endpoints; ports listed (email/sms/renderer drivers).
- **Check:** `/internal/manifest` lists notification ports + endpoints.

### Step 7.9 — Tests
- Unit: safe renderer (`$var` works; **injection string is inert** — no eval; missing var errors), failover (falls through, all-fail returns false), preference suppression, locale fallback, delivery recording.
- Integration: `/internal/notify` (token required; send → delivery recorded), in-app feed + mark-read, preferences GET/PUT, template admin (cap-gated).
- Security: **no template injection/SSTI**, http provider **rejects non-allowlisted host** (SSRF), recipient validation, feed never leaks internal fields, internal endpoint rejects missing token.
- **Check:** all green.

### Step 7.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: seed a template → `POST /internal/notify` (console email) → 200 → delivery recorded `sent`; create in-app item → `GET /api/v1/notifications` shows it.
- **Check:** end-to-end works.

### Step 7.11 — Docs
- Service `README.md`: what it is, endpoint table, config knobs, plug-and-play channel/provider matrix, failover + templates + preferences.
- Update `docs/architecture-plan.md` (notification ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **Push (FCM/APNs), web-push** → later channel adapters (same ports pattern).
- **Outbound signed webhooks** → webhook-service (Tier 2).
- **Open/click tracking pixels, bounce/complaint ingestion** → delivery-tracking extension.
- **DKIM/SPF/DMARC** → DNS/infra concern, documented, not code.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #65 | 7.1 Skeleton | ✅ completed |
| #66 | 7.2 Config | ✅ completed |
| #67 | 7.3 Ports | ✅ completed |
| #68 | 7.4 Adapters + failover + registry | ✅ completed |
| #69 | 7.5 Domain models + repos | ✅ completed |
| #70 | 7.6 Core NotificationService | ✅ completed |
| #71 | 7.7 Routes | ✅ completed |
| #72 | 7.8 Manifest & wiring | ✅ completed |
| #73 | 7.9 Tests | ✅ completed |
| #74 | 7.10 Verify + live boot | ✅ completed |
| #75 | 7.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (140 tests; gate PASS; live boot OK) |

**Providers shipped (verified against official docs):** Email — console, smtp, resend,
sendgrid, mailgun, postmark. SMS — console, twilio, vonage, unifonic (KSA), taqnyat (KSA),
msegat (KSA), mobily (KSA), webhook (any). 10 real providers, KSA-focused.

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play (the point)

| Choice | Knob | Options |
|---|---|---|
| Email delivery | `EMAIL_CHAIN` | `console` · `smtp` · `smtp,console` (failover) |
| SMS delivery | `SMS_CHAIN` | `console` · `http` · `http,console` |
| Storage | `DATASTORE_DRIVER` | memory · postgres |
| Default language | `DEFAULT_LOCALE` | any locale key |

Same code, any provider — add an adapter, list it in the chain. Other services just
`POST /internal/notify`; they never know which provider actually delivered.

_When every box is ✅, notification-service is live. Next Tier-1: tenant · authz · api-gateway · file · audit._
