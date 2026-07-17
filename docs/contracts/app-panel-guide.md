# End-user app — integration guide

One endpoint drives the whole end-user app: `GET /api/v1/app/catalog` on the api-gateway. It is the
user-facing twin of the admin panel — same aggregator, same rules — so this guide is short and points
at the admin guide for the shared mechanics.

- Machine contract: [`app-panel.openapi.yaml`](./app-panel.openapi.yaml)
- Per-action schema: [`user-actions.schema.json`](./user-actions.schema.json) (identical shape to `admin-actions.schema.json`)
- Front-end render/invoke mechanics: [`admin-panel-guide.md`](./admin-panel-guide.md) — the same code, just point it at `/api/v1/app/catalog`.

## What differs from the admin panel

| | admin panel | end-user app |
|---|---|---|
| endpoint | `GET /api/v1/admin/catalog` | `GET /api/v1/app/catalog` |
| declaration | each service's `app/admin_actions.py` → manifest `admin_actions` | each service's `app/user_actions.py` → manifest `user_actions` |
| who may open | operators only (`ADMIN_CATALOG_CAP`, default `platform.admin`) | **any authenticated user** — no open-cap |
| what it shows | platform/operator routes | the user's **own** self-service routes |
| filtering | to the caller's caps | to the caller's caps (same) |

Everything else is identical: the catalog is a **map, never the authority** (every action is
re-checked AND re-scoped to the caller's own tenant/identity at its route), discovery is config
(`ADMIN_SERVICES`, the same list feeds both), the internal token stays server-side, declarations are
untrusted input (method allow-listed, path forced relative), and results are filtered to the caller's
capabilities.

## For a service: appear in the end-user app

Declare `app/user_actions.py` (a PURE literal, same shape as `admin_actions`) and the manifest emits it
under `user_actions` (the `_template` wires this automatically). Declare only the user's OWN
self-service routes — `_SELF` / ordinary user-cap routes (their files, their profile, their
subscription). Operator (`platform.*`) routes belong in `admin_actions`; internal-token routes belong
nowhere. The `safety:user-actions` CI gate enforces R1–R6 (route exists, path relative, method
allow-listed, id/cap/label well-formed, capability advertised, manifest emits the field) — mutation-
tested, exactly like `safety:admin`.

## Two panels, one platform

A service can appear in **both**: its operator routes (e.g. `platform.stream`) in the admin panel, and
its tenant-scoped self-service routes (e.g. `stream.view` / `stream.manage`) in the end-user app — the
same routes, filtered by which capability the caller holds. Services with no end-user surface
(compliance, authz, feature-flag, api-gateway) declare an explicit empty `user_actions`, so the absence
is a reviewed decision, not a forgotten file.
