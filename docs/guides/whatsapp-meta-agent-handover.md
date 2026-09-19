# WhatsApp → Meta Business Agent handover — integration guide

**Audience:** a platform (e.g. Raje3) moving a WhatsApp number's ordinary chat to Meta Business
Agent, keeping full customer privacy. All knobs default to **privacy-first**; a platform opts a number
into being handled by us. Verified against Meta docs 2026-09-19 (see the plan doc for sources).

## The model (why two layers, enforced by us)

Meta Business Agent, once enabled, is the **primary responder** — it answers the customer and calls
your Connectors for actions; you get actions + handoff events, not raw chat. But public docs do NOT
guarantee Meta sends us *zero* message webhooks, so **we do not trust Meta to withhold** — privacy is
enforced on our side, in two independent layers:

- **Layer 2 — never RECEIVE (Meta-side + our-side):** point the number's webhooks away from us
  (webhook override), and our webhook drops any content that still arrives at the root.
- **Layer 1 — never STORE:** even content that entered is never persisted.

The unit is the **number** (`phone_number_id`) — Meta has no per-conversation handoff on WhatsApp.

## The switch: handover mode per number

`mode = us | meta`, resolved as: **global hard override → per-number setting → deployment default**.

| Knob | Default | Meaning |
|---|---|---|
| `WHATSAPP_DEFAULT_HANDOVER_MODE` | `meta` | baseline for every number without its own setting. `meta` = private (dropped at root); `us` = we handle it. Flip to move all not-individually-set numbers at once. |
| per-number override | (unset) | `POST /internal/whatsapp/handover-mode {phone_number_id, mode}` — beats the default for one number. |
| `WHATSAPP_INBOUND_PRIVACY_MODE` | `false` | global HARD override: when true, every number is private regardless of per-number/default. |

**Fail-closed:** if the per-number lookup errors, the number is treated as **private** (content
dropped) — a hiccup never leaks. The webhook is still acked 200 (Meta won't retry-storm).

## Storage knobs (only relevant for `us` numbers)

| Knob | Default | Meaning |
|---|---|---|
| `WHATSAPP_CONVERSATION_STORE_ENABLED` | `false` | store inbound + coexistence content at all (the durable catch-up feed is opt-in). |
| `WHATSAPP_INBOUND_RETENTION_DAYS` | `30` | retention for `wa_inbound` (a daily purge reaps expired; DSAR erase covers it). |
| `WHATSAPP_COEXISTENCE_ENABLED` / `_RETENTION_DAYS` | `false` / `2` | the owner's own-app activity (echoes/history/contacts). |

A `meta` number never reaches a store (content is dropped first), regardless of these.

## Operator routes (internal token)

- **Handover mode:** `GET /internal/whatsapp/handover-mode?phone_number_id=…` → `{mode, source, private}`;
  `POST /internal/whatsapp/handover-mode {phone_number_id, mode}`; `POST …/handover-mode/clear`.
- **Meta-side webhook override (the actual reroute):** `POST /internal/whatsapp/webhook-override
  {tenant, scope: phone|waba, callback_uri, verify_token}` and `POST …/webhook-override/clear
  {tenant, scope}`. The target must be HTTPS and its host in `WHATSAPP_OVERRIDE_ALLOWED_HOSTS`
  (empty = fail closed); every set/clear is audited critical; the verify_token is never returned.
  Verified Meta shapes: `POST /{phone_number_id}` `webhook_configuration.override_callback_uri`
  (clear = `""`); `POST /{waba_id}/subscribed_apps` `override_callback_uri` (clear = no body).

## Recipe — hand a number to Meta (and back)

1. Set the number private on our side: `POST /internal/whatsapp/handover-mode {phone_number_id, "meta"}`
   (or rely on the `meta` default). Our webhook now drops that number's content at the root.
2. Route it at Meta: add the number's endpoint to `WHATSAPP_OVERRIDE_ALLOWED_HOSTS`, then
   `POST /internal/whatsapp/webhook-override {tenant, "phone", <meta endpoint>, <verify token>}`.
3. Connectors/tools keep working (separate routes) — that's what the agent calls for real actions.

**Hand back:** `POST …/webhook-override/clear` then `POST …/handover-mode {…, "us"}` (and enable
storage if you want the feed). Clean and idempotent.

## What still flows for a `meta`/private number
Delivery/read **receipts** (about our own sends), **account/template** events, the **payment**
status, and the **user_preferences opt-out** (a compliance signal that must always be honoured).
Never: inbound customer messages, coexistence content, contact profiles.
