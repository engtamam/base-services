# Phase 33 — One admin panel, and every service declares its own controls

> **Target release: v0.5.0.** This is the showcase feature — the one that proves the whole
> architecture: seventeen self-contained services, no shared library, no central registry, and yet
> one coherent operator surface that a customer can extend with their own product service by
> implementing a contract. It has to be the best of our work: general (built for a contract, not our
> 17), plug-and-play (discovery by config), and secure by construction (the panel is a map, never
> the authority).

A platform operator needs one place to *do the admin things*: suspend a tenant, refund an invoice,
resend an invite, rotate a role, unblock a rate limit, drain the notification queue. Today those
live as seventeen separate internal routes with no map. The obvious move — one big file listing
every admin action — is the wrong one: it rots the first time a service adds a feature and nobody
updates the file, and then the panel lies.

So we invert it. **Each service declares its own admin actions, in itself.** A small aggregator asks
every service "what can an admin do here?" and merges the answers. The panel renders the merged
list. Nobody maintains a master file, and the panel cannot drift from the code, because the code is
the source.

And it is not built for our seventeen. It is built for a **contract**: any service — ours, or a
customer's own product service, in any language — that answers the manifest with the `admin_actions`
shape appears in the panel automatically. Plug-and-play by contract, discovery by config.

---

## The invariant that makes this safe

**The panel is a MAP, never the AUTHORITY.**

Showing a button is not permission to press it. Every admin action is an existing, capability-gated
route on the owning service, and that route re-checks the capability **server-side** on every call.
The catalog only decides what a user is *shown*; the service decides what they may *do*. If any
service ever trusts "the panel let me through," it is broken — and the gate in §6 refuses exactly
that.

```
service           declares its admin_actions   (in /internal/manifest — the code is the source)
aggregator        discovers services by CONFIG, fans out, merges, filters by the caller's caps
panel (UI)        renders forms; every submit hits the OWNING service, which re-authorizes
```

---

## The contract (the actual product)

Each service adds an `admin_actions` array to its existing `/internal/manifest`:

```json
{
  "service": "tenant-service",
  "admin_actions": [
    {
      "id": "tenant.suspend",
      "label": "Suspend tenant",
      "method": "POST",
      "path": "/api/v1/tenants/{tenant_id}/suspend",
      "capability": "platform.tenants",
      "params": [{ "name": "tenant_id", "in": "path", "type": "string", "required": true }],
      "confirm": true,
      "danger": true
    }
  ]
}
```

`capability` is what the route already enforces — declared here only so the panel can hide a button
the caller could not use anyway. `params` become form fields. `confirm`/`danger` are UX hints for
destructive actions; they are **not** a security control (the server is). The shape is published as
a versioned spec (`docs/contracts/admin-actions.schema.json`) so a customer's service can implement
it without reading our code.

---

## 1. Definition of Done

### (1) Is this on the hot path?

No. The catalog is an admin-time read — fetched when an operator opens the panel, not per user
request. It is **cached** (`ADMIN_CATALOG_TTL`, default 60s) so opening the panel does not fan out
to every service each time. A stale entry is self-correcting: a removed action 404s on submit and
the panel refetches. No benchmark; there is no hot path to protect.

### (2) Does every config knob have a consumer?

`ADMIN_SERVICES` (discovery — a list, a label selector, or a registry file), `ADMIN_CATALOG_TTL`,
and each service's `admin_actions` (declared, not central). The knob-guard test enforces the
consumer, as always.

### (3) What production needs that "list the routes" omits

* **Filter by the caller's capabilities.** The catalog returns only actions the caller could
  actually perform. Otherwise the panel leaks the platform's entire capability vocabulary to anyone
  who opens it (T5), and shows buttons that will only ever 403.
* **The aggregator holds the internal token; the browser never does.** The panel talks to the
  aggregator; the aggregator talks to services with the internal token. A token that reaches a
  browser is a token that reaches everyone.
* **Destructive actions are flagged AND re-confirmed** — but that is UX. The delete still tombstones
  and the refund still requires `platform.billing` at the service.
* **Every admin action is already audited**, because it goes through the service's own route, which
  already writes to the audit chain. The panel adds no new write path — a deliberate non-feature.

### (4) The correctness footgun

**A service trusting the panel instead of the token.** The whole design collapses the instant one
service says "this came from the admin panel, so it must be allowed." The panel is not an identity.
Every action carries the operator's own token, and every route re-runs its capability check exactly
as if the call had come from `curl`. The gate asserts no admin route is reachable without its
capability, panel or no panel.

---

## 2. Steps — one service at a time, so we learn every admin thing it can do

The point of going service-by-service is discovery: cataloguing a service's admin surface forces us
to *find* every operator action it has (and the ones it is missing). Each step is: read the service,
enumerate its admin routes, declare them, and note any gap.

| # | Step |
| --- | --- |
| 33.1 | **The contract + aggregator + discovery.** `admin-actions.schema.json`; `GET /internal/admin/catalog` on api-gateway (fans out by `ADMIN_SERVICES`, merges, filters by caller caps); the `admin_actions` field on the manifest builder. **GATE `safety:admin`** |
| 33.2 | **tenant-service** — provision, config, branding, suspend, reactivate, delete (tombstone), export |
| 33.3 | **billing-service** — plans, coupons, refund, run-dunning, **settle**, subscription change/cancel |
| 33.4 | **identity-service** — registration toggle, add/remove member, invite, force-logout (revoke family), tenant admin |
| 33.5 | **authz-service** — create/edit role, edit policy, assign roles, resolve-effective (read) |
| 33.6 | **compliance-service** — open a DSAR, view its acks, retention policy |
| 33.7 | **notification-service** — templates, drain queue, view dead-letters, resend |
| 33.8 | **file-service** — quarantine review, force blob GC, quota |
| 33.9 | **wallet-service** — adjust balance (with reason), freeze, view ledger |
| 33.10 | **audit-service** — verify chain, export, search (read-only — it must stay append-only) |
| 33.11 | **feature-flag-service** — toggle a flag, targeting rules |
| 33.12 | **webhook-service** — endpoints, replay a delivery, view failures |
| 33.13 | **connector-service** — enable/disable a connector, rotate its secret |
| 33.14 | **search-service** — reindex, view index health |
| 33.15 | **job-service** — retry / cancel a job, view the queue |
| 33.16 | **analytics-service** — prune raw, view rollup health |
| 33.17 | **zatca-service** — resubmit an invoice, view clearance status |
| 33.18 | **api-gateway** — rate-limit unblock, route health (and it hosts the aggregator) |
| 33.19 | The panel spec: a reference renderer contract (the UI is the consumer's job, but we ship the OpenAPI + a worked example so any front end can build it) |

## 3. Out of scope

**The UI itself.** We ship the catalog API and the contract; the actual admin front end is the
consumer's to build, exactly as every other UI in this platform is. A worked example and the schema
are the deliverable, not a React app.

**A second permission system.** The panel introduces no new capabilities. It reuses each service's
existing caps. If an action needs a new cap, that is the service's phase, not this one.

## 4. What this design refuses to do

**It will not let the manifest name an arbitrary URL to call.** A customer's service declares a
`path` on *itself*; the aggregator always calls it at *that service's* discovered address, never a
host the manifest supplies. A declaration that tries to point elsewhere is ignored. Otherwise the
admin panel becomes an SSRF console (T2).

---

## 5. Threat model — an admin panel is a master key; treat it like one

### T1 — The panel treated as the authority  (CWE-602, CWE-863) — CRITICAL

If a service trusts "the request came from the admin panel," anyone who can reach the panel's egress
is an admin. *Mitigation:* the panel is a map. Every action carries the operator's own token and
every route re-checks its capability server-side — the same check a raw `curl` would face. Gated.

### T2 — Aggregator SSRF via discovery or a hostile manifest  (CWE-918) — CRITICAL

The aggregator fans out to service addresses. If those addresses (or a `path`/host inside a
declared action) come from a request or from the manifest itself, it is an SSRF pivot into the
cluster. *Mitigation:* targets come from **config only** (`ADMIN_SERVICES` — an allowlist, a label
selector, or a registry file), never from a caller; a declared action is always invoked at its
owning service's known address, and any URL/host field in a declaration is ignored.

### T3 — The internal token reaching the browser  (CWE-522) — HIGH

The aggregator uses the internal service token to call services. If the panel (a browser app) ever
holds it, every user holds it. *Mitigation:* the browser talks only to the aggregator with the
operator's user JWT; the aggregator holds the internal token server-side and never returns it.

### T4 — A customer's service declaring a hostile action  (CWE-79, CWE-601) — HIGH

The panel renders forms from `admin_actions` — including a third party's. A malicious `label` is
stored-XSS; a crafted `path` is an open redirect or a call to the wrong route. *Mitigation:*
declarations are untrusted input. Labels are escaped by the renderer contract; `method` is an
allow-list (`GET/POST/PUT/DELETE`); `path` must be a relative path on the declaring service
(no scheme, no host, validated exactly like the edge validator refuses a Caddyfile injection).

### T5 — Capability enumeration through the catalog  (CWE-200) — MEDIUM

An unfiltered catalog tells any operator every capability the platform has, and who can do what.
*Mitigation:* the aggregator returns only actions whose `capability` the caller holds. Same discipline
as the members directory — you see your own doors, not the building's blueprint.

### T6 — Destructive action without confirmation  (operator error) — MEDIUM

A one-click "delete tenant" next to "view tenant" is an outage waiting for a mis-click.
*Mitigation:* `danger`/`confirm` on the declaration; the renderer contract requires a re-confirm for
`danger` actions. UX, not security — the server still enforces the cap.

### T7 — Stale catalog / version skew  (correctness) — LOW

A cached catalog offers an action a service has since removed. *Mitigation:* the submit 404s at the
service (the truth), and the panel refetches. The catalog is a hint, never the contract — the same
rule as every cache in this platform.

## 6. The gate: `safety:admin`

The panel is a master key, so its wiring gets a gate, mutation-tested:

1. Every declared `admin_action` maps to a **real route** on that service with the **capability it
   declares** — a phantom action, or one whose real route enforces a different cap, is a lie the
   panel would render.
2. No `admin_action` `path` carries a scheme or host — it is a relative path on the declaring
   service (T2/T4).
3. The aggregator's targets come from **config**, never from a request parameter (T2).
4. The catalog response is **filtered by the caller's capabilities** — a fixture caller with no caps
   gets an empty catalog, not the full map (T5).
5. `method` is one of the allow-listed verbs; `label`/`params` are declared as data, never
   interpolated into a URL.

---

**STATUS: 🔵 PLANNED.**
