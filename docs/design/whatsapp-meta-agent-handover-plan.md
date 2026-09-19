# WhatsApp → Meta Business Agent handover — phased plan

**Status:** planning (P0). **Owner:** Tamam. **Started:** 2026-09-19.
Consuming app: Raje3. This is plug-and-play **infrastructure** in notification-service — every step is
a config knob so each platform picks what it wants; nothing here changes another platform's behavior.

## Goal

Let a platform hand a WhatsApp number's ordinary chat to **Meta Business Agent** (Meta answers the
customer directly; we only provide Connectors/tools for real actions), with:

1. **on/off** — switch a number between `us` (today's path) and `meta` (agent holds it), reversible
   at any time for any reason.
2. **Two independent privacy layers when on `meta`, both enforced BY US (never trusting Meta):**
   - **Layer 1 — never STORE:** no customer/shop message content is ever persisted.
   - **Layer 2 — never RECEIVE:** the message never enters our pipeline at all (dropped at the root,
     and routed away at Meta's side).

## Verified facts (Meta docs, 2026-09-19 — re-verify at build)

- Meta Business Agent, once enabled, is the **primary responder**: it answers the customer, calls
  your Connectors/APIs for actions, and hands off to your app "when needed" — you receive **actions +
  handoff events, not raw chat**. [overview]
- Concrete routing lever: **per-number webhook override** — `POST /{PHONE_NUMBER_ID}` with
  `{"webhook_configuration":{"override_callback_uri","verify_token"}}` routes that number's `messages`
  (and other supported fields) to a chosen endpoint instead of our app default. Per-WABA twin:
  `POST /{WABA_ID}/subscribed_apps` with `override_callback_uri`. Priority: phone → WABA → app
  default. Account/template webhooks always go to the app default. [webhooks/override]
- **Honest gap:** public docs do NOT guarantee Meta sends us *zero* message webhooks while the agent
  is primary (Business Agent Platform API is partly gated). **This is exactly why Layer 1 + Layer 2
  are enforced on our side** — our guarantee must not depend on trusting Meta's withholding.
- Per-conversation handoff is NOT supported on WhatsApp (only per-number / MSC, closed beta) — see
  [[whatsapp-commerce-payments-plan]] research note. So the unit of on/off is the **number**.

Sources: developers.facebook.com/documentation/meta-business-agent/overview ·
/business-messaging/whatsapp/webhooks/override/ · /solution-providers/multi-solution-conversations

---

## Phases — small, independently shippable steps (each: test → gates green → release v0.25.x → notify)

### Phase A — Layer 1: the never-store switch  *(Raje3's direct ask; smallest, ship first)*
- **A1** config `WHATSAPP_CONVERSATION_STORE_ENABLED: bool = False` (deployment-level; **default OFF**
  — house rule: a new capability starts off and the platform that wants it enables it, and here that
  also makes privacy the default. Consequence: the durable inbound catch-up feed
  (`/internal/whatsapp/inbound`) is now opt-in — a platform that reads customer replies from it sets
  this True; Raje3 leaves it off).
- **A2** in `webhooks.py::receive`, when the switch is OFF: pass `inbound=None` (no `wa_inbound`
  write) — the fan-out already guards `inbound is not None`; Meta is still ack'd, bus still publishes.
- **A3** same switch also forces coexistence OFF (no echoes/history/contacts write), on top of the
  existing `WHATSAPP_COEXISTENCE_ENABLED`.
- **A4** tests: OFF ⇒ zero rows in wa_inbound + coexistence; still returns received:true; bus event
  still emitted. ON ⇒ unchanged.
- **A5** release + tell Raje3.

### Phase B — Layer 2 (our side): full-privacy receive-drop
- **B1** config `WHATSAPP_INBOUND_PRIVACY_MODE: bool = False` (deployment-level; when a number is on
  meta). Distinct from A: A stops STORING; B stops the content ENTERING our pipeline at all.
- **B2** when ON: `receive`/`_fan_out` drop inbound customer **message content** before any store OR
  bus publish — we ack Meta (so no re-delivery storm) and do nothing else with it. We "know nothing."
- **B3** what still flows (must NOT be dropped): delivery/read **status** receipts, **account** &
  **template** events (Meta always sends these to us anyway), and the **Flow/connector** callbacks
  (separate routes, not this webhook) — the agent's actions depend on them.
- **B4** tests: privacy-mode ON ⇒ an inbound `messages` payload yields no store, no `whatsapp.message`
  bus event, but still 200 + status/account events still fan out.
- **B5** release.

### Phase C — the Meta-side transition control (per-number webhook override)
- **C1** re-verify the two override shapes against current Meta docs, dated note.
- **C2** `whatsapp_admin`: `set_phone_webhook_override(...)` / `clear_phone_webhook_override(...)`
  (POST /{phone_id} webhook_configuration; clear = empty override), + the per-WABA twin. Blocking
  Graph calls off the event loop; distinct-vocabulary errors like the other admin calls.
- **C3** internal route `POST/DELETE /internal/whatsapp/webhook-override` (per phone/WABA), gated by
  the internal token, **audited critical** (rerouting a shop's message delivery is high blast radius),
  and refused unless the target host is in a **config host-allowlist** (`WHATSAPP_OVERRIDE_ALLOWED_HOSTS`)
  — an operator must not point a shop's messages at an arbitrary URL.
- **C4** manifest route + auditable action + OpenAPI regen.
- **C5** tests: set/clear, allowlist reject, audit written, verify_token never returned.
- **C6** release.

### Phase D — orchestrate the on/off (tie A+B+C into one switch per number)
- **D1** a per-number mode store `handled_by = us | meta` (default `us`), tenant/number-scoped.
- **D2** set→`meta`: apply the webhook override (C) **and** turn on privacy-drop (B) for that number.
- **D3** set→`us`: clear the override **and** restore normal receive/store — clean, idempotent.
- **D4** operator surface (GET/PUT) to flip + read the current mode per number; audited.
- **D5** edge cases: toggle race (messages in flight during the switch — Layer 1/2 already swallow
  them), Meta re-delivery, a number with no override support, partial failure (override set but store
  flip failed → fail closed to privacy, never to storing).
- **D6** release.

### Phase E — close-out
- End-to-end doc (how a platform switches a number to the agent and back), final gate sweep (all 55 +
  any new gate for the override class), notify Raje3 with the full contract.

## Release policy for THIS plan (Tamam, 2026-09-19)
- **No release until ALL phases (A–E) are done.** Commit locally per step; cut ONE release at the end.
- **Re-verify the relevant Meta official docs at the start of every build step**, with a dated note —
  Meta's WhatsApp surface changes by region/version; never build against a stale shape.

## Definition of done (per step)
Deep tests incl. the privacy assertions · all fleet gates green · path-scoped commit · its own
release within v0.25.x · Meta shapes verified with a dated note · never trust Meta for a privacy
guarantee — enforce it on our side.
