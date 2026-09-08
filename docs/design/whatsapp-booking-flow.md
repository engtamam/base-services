# Design — WhatsApp Booking Flow (config-driven, 100% customizable)

Status: BUILT (core), OFF by default. Author: notification-service work.
Verified against Meta's WhatsApp Flows docs on **2026-09-08** (re-verify before changes — Meta revises
Flow JSON versions and the endpoint contract; cite the version you build against).

## 0. Implementation status + boundary (updated 2026-09-08)

**Boundary (firm): we ship INFRASTRUCTURE; each platform connects + designs per its own philosophy.**
We do NOT: generate keys, upload anything to Meta, manage WABA numbers, or impose routing/topology. A
platform generates its own keys and uploads the public key + Flow JSON to Meta ITS OWN WAY (it already
owns Embedded Signup / tokens); a tenant may have many WhatsApp numbers (per branch) — its routing.
Nothing we build may block even one platform's scenario.

**Built as a GENERIC, opt-in ladder — any dynamic (`data_exchange`) Flow reuses it; booking is one
handler.** Each rung is usable alone; none is required:
- **L1 `core/flow_crypto.py`** — decrypt/encrypt only (RSA-OAEP-SHA256 + AES-128-GCM, reply IV
  bit-inverted). Pure, no keys/state.
- **L2 `core/flow_dispatch.py`** — `process()` one-call: decrypt → `ping` (answered here, cheap) →
  handler → encrypt. `FlowHandler` protocol. `tenant` is optional handler context (kept out of L2 →
  no routing imposed).
- **L3 `core/flow_registry.py`** — name → FlowHandler; one endpoint serves several dynamic Flows.
- **L4 `core/booking_flow.py`** — `BookingEngine` (the only calendar-specific piece) + the provider
  **contract lives in the port docstrings** (AvailabilityProvider/BookingProvider) — the integrator
  reads the code, not a separate doc.
- Public routes `POST /webhooks/whatsapp/flow[/{tenant}]` — verify X-Hub-Signature-256 before decrypt,
  resolve the shop's platform-sealed private key by tenant (tenant in the URL: the key must be picked
  before the payload can be read), 421 on decrypt failure.
- `build_flow(flow_action="data_exchange")` sends a dynamic Flow (generic — any flow, not booking).
- `core/flow_json.py` + `GET /internal/whatsapp/flow/booking-json` — generates the companion Flow JSON
  (screens/fields from the same engine constants, can't drift) for the platform to upload itself.
- Adapters `adapters/booking/{http,null,config_defs}`; config `WHATSAPP_FLOW_*` / `WHATSAPP_BOOKING_*`.

The original design below is retained for the full rationale; sections 4/7 are realised by the ladder.

## 1. Goal

Provide **infrastructure**, not a product. A platform must be able to build any appointment/booking
experience *inside WhatsApp* — pick a service, pick a staff member (a specific doctor), pick a
location/room, pick a date, pick a time — where **each resource has its own schedule** and the set
and order of steps is entirely the platform's choice. We own the hard, generic parts (encryption,
the screen state machine, calendar-constraint wiring, session correlation, double-booking safety);
the platform owns its domain (who is free, when) behind two small ports. We never encode "doctor",
"salon", or any tenant's business rule.

Non-goal: we do not decide the steps, the resources, the working hours, or how availability is
computed. Those are the platform's, supplied via config + ports.

## 2. What WhatsApp gives us (verified 2026-09-08)

WhatsApp **Flows** render a **fixed set of form components** — no HTML/canvas/animation. The ones
that matter here:

- **`CalendarPicker`** (Flow JSON v6.1+) — the strong one:
  - `mode`: `"single"` | `"range"` (dynamic-bindable) — single appointment vs a stay/rental range.
  - `min-date` / `max-date` — `"YYYY-MM-DD"`, **dynamic** via `${data.…}`.
  - `unavailable-dates` — array of `"YYYY-MM-DD"`, **dynamic** — the fully-booked days.
  - `include-days` — allowed weekdays, e.g. `["Sun","Mon","Tue","Wed","Thu"]` — the working days.
  - `min-days` / `max-days` — range mode only (min/max nights).
  - `on-select-action`: only `data_exchange` — i.e. selecting a date can call **our server**.
- **`DatePicker`** — single date; same `min-date`/`max-date`/`unavailable-dates`; simpler fallback
  for tenants below v6.1.
- **`Dropdown` / `RadioButtonsGroup` / `ChipsSelector`** — the time-slot and resource pickers; their
  options are **dynamic** (`${data.…}`).
- **`Footer`** button with `on-click-action: data_exchange` — submits a screen to our server.
- **`TextHeading/Subheading/Body/Caption`, `RichText`, `Image`, `If`, `Switch`** — labels, summary,
  conditional screens. Max 50 components/screen.

The load-bearing fact: **every screen's data is whatever our endpoint returns**. That is what makes a
generic, config-driven engine possible.

## 3. The endpoint contract (verified 2026-09-08)

Our endpoint is a **server-driven state machine**. Decrypted request:

```json
{ "version": "3.0", "action": "INIT|BACK|data_exchange|ping",
  "screen": "<CURRENT_SCREEN>", "data": { /* the fields the user just submitted */ },
  "flow_token": "<session id we generated when sending the Flow>" }
```

- `INIT` — user opened the Flow (when sent with `flow_action: data_exchange`).
- `data_exchange` — a screen was submitted (a date picked, a slot chosen, confirm pressed).
- `BACK` — back pressed (only if `refresh_on_back: true`).
- `ping` — health check → reply `{"data":{"status":"active"}}`.

Navigation response (non-terminal):
```json
{ "screen": "<NEXT_SCREEN>", "data": { /* components' dynamic data */,
                                       "error_message": "<optional snackbar>" } }
```
Terminal response (finish + post back to chat):
```json
{ "screen": "SUCCESS", "data": { "extension_message_response": {
    "params": { "flow_token": "<same>", "booking_ref": "..." } } } }
```

Encryption (per Meta): request carries an RSA-OAEP-encrypted AES key
(`RSA/ECB/OAEPWithSHA-256AndMGF1Padding`, SHA-256 MGF1) + AES-**GCM** payload (128-bit tag appended);
the response is AES-GCM with the **same** key and a flipped IV. Each tenant (WABA) has its own RSA key
pair; the public key is uploaded to Meta, the private key stays sealed in our secret store.

## 4. Architecture — three pieces

### 4.1 Flow definition (per-tenant config, no code)

A declarative description the platform edits from its panel:

```jsonc
{
  "horizon_days": 30,               // how far ahead a customer may book
  "min_notice_minutes": 120,        // earliest bookable slot from "now"
  "slot_granularity_minutes": 30,   // display/booking grid (provider may override per resource)
  "default_working_days": ["Sun","Mon","Tue","Wed","Thu"],
  "mode": "single",                 // or "range" for stays/rentals
  "range": { "min_days": 1, "max_days": 14 },   // range mode only
  "locale": "ar",                   // all labels come from `strings` below (i18n)
  "steps": [                        // ORDER + PRESENCE are the platform's choice
    { "dimension": "service",  "required": true },
    { "dimension": "staff",    "required": false },   // "any available" allowed when optional
    { "dimension": "location", "required": false },
    { "dimension": "date" },
    { "dimension": "time" },
    { "dimension": "confirm" }
  ],
  "strings": { "pick_staff": "اختر الطبيب", "pick_date": "اختر اليوم", ... }
}
```

- Drop `staff` → single-resource shop. Reorder freely. `date`/`time`/`confirm` are the terminal spine;
  any number of selection `dimension`s precede them.
- We validate the definition at write time (a step referencing an unknown dimension, `range` without
  `min/max_days`, `horizon_days` out of Meta's cap, etc.) — fail loud, never a broken Flow at runtime.

### 4.2 Two ports the platform implements (their domain lives here)

```python
class AvailabilityProvider(Protocol):
    # options for a selection step, given what's chosen so far (service→staff filtering, etc.)
    async def options(self, *, tenant, dimension, chosen: dict) -> list[Option]  # [{id,title,description?}]
    # the calendar constraints for THIS resource selection
    async def calendar(self, *, tenant, chosen: dict) -> CalendarConstraints
        # -> {min_date, max_date, unavailable_dates:[...], include_days:[...]}
    # the free time slots for a chosen date (single) or the range validity (range mode)
    async def slots(self, *, tenant, chosen: dict, date: str) -> list[Slot]     # [{id,label,start,end}]

class BookingProvider(Protocol):
    async def reserve(self, *, tenant, selection: dict, idempotency_key: str) -> BookingResult
    async def cancel(self, *, tenant, booking_ref: str) -> bool
```

- "Each doctor has a different schedule" is **not our concern**: `staff_id` is in `chosen`, so
  `calendar()`/`slots()` return *that doctor's* free days/times. Multiple staff with disjoint
  availability = the provider's own logic.
- We ship a **default HTTP adapter** (calls the platform's own service over the internal tier /
  a configured URL) and a **null** adapter, exactly like every other port in this repo.

### 4.3 The generic engine (we own it)

On each request:
1. Resolve the tenant from the Flow (WABA/phone id), decrypt with that tenant's sealed private key.
2. Load/lay down **session state** keyed by `flow_token` (+tenant), short TTL: the accumulated
   `chosen` selections, so the client never re-sends prior steps and the wire stays minimal.
3. Dispatch on `(action, screen)`:
   - `INIT` → render the first `step`'s screen (options from `options()`, or the calendar if the
     first step is `date`).
   - `data_exchange` → merge the submitted field into `chosen`; if more selection steps remain,
     render the next (`options()`); when reaching `date`, render the CalendarPicker filled from
     `calendar(chosen)`; on date submit, render the time screen from `slots(chosen, date)`; on time
     submit, render the confirm summary; on confirm, call `reserve()` and return `SUCCESS`.
   - `BACK` → pop the last selection from `chosen`, re-render the prior screen.
   - `ping` → active.
4. Encrypt the response with the request's AES key (flipped IV).

## 5. Correctness & security (from our own scars)

- **Double-booking is our double-spend.** `reserve()` is called through the atomic **claim** pattern
  (`cas` put-if-absent, see wallet `_claim` / billing `IdempotencyStore.claim`) keyed on
  `(tenant, resource, slot)` — two customers racing for the last slot: exactly one wins; the loser
  gets a clean "just taken" and the calendar re-renders. The idempotency key also dedupes a
  redelivered final `data_exchange`.
- **Session isolation.** Session rows are keyed by `flow_token` **length-prefixed with the tenant**
  (see the tenant-scoped-idempotency rule), TTL'd, and never readable across tenants.
- **Per-tenant keys.** RSA private keys sealed in the secret store, one per WABA; rotation supported
  (Meta allows uploading a new public key; overlap during rotation).
- **Fail closed.** A provider timeout/error → a snackbar `error_message` and the same screen, never a
  silent empty calendar that looks like "no availability". `ping` must always answer.
- **Stale availability.** The calendar shows `unavailable_dates` at render time, but truth is at
  `reserve()`: a day that filled between render and confirm is caught by the atomic claim, not by the
  calendar. We never trust the client's view for the money/slot decision.
- **PII.** Names/phones from the Flow follow the existing WhatsApp inbound PII handling; the session
  stores ids, not raw personal data, where avoidable.

## 6. Worked examples (all the SAME engine + config)

- **Clinic, choose a doctor:** steps `[service, staff, date, time, confirm]`, `mode:single`.
  `options(staff, {service})` lists doctors for that service; `calendar({service,staff})` returns
  that doctor's working days + full days; `slots(...,date)` their free times.
- **Single-chair barber:** steps `[service, date, time, confirm]` — no `staff`.
- **Hotel / car / equipment rental:** `mode:range`, steps `[service(room type), date, confirm]`,
  `min_days/max_days`; `calendar` returns unavailable ranges.
- **Restaurant table:** steps `[location(branch), date, time, confirm]` + a `party_size` Dropdown
  as an extra selection step (a generic `dimension` whose options are static from config).

## 7. Build plan (staged, each shippable)

1. **Crypto + endpoint skeleton** — decrypt/encrypt, `ping`, tenant resolution, per-WABA keys.
   Locked tests with Meta's published test vectors.
2. **State machine + session** — INIT/data_exchange/BACK over the config spine, server-side session.
3. **Ports + default HTTP/null adapters** — `AvailabilityProvider`, `BookingProvider`; validation of
   the flow definition at write time.
4. **Atomic reserve** — the claim-based double-book guard + idempotent final submit.
5. **Panel config + i18n + admin actions/audit** — create/update a booking flow, upload the WABA key.

## 8. Open questions for review

- Session store: reuse notification-service's datastore, or a dedicated short-TTL store?
- Where does `reserve()` live by default — the platform's own service (HTTP port) is the assumption;
  do we also want a built-in adapter that books into a first-party calendar service if one exists?
- Do we expose the flow-definition as runtime config (per-tenant, hot-editable) or deploy-time?
  (Leaning runtime, per the platform-picks-via-config philosophy.)
