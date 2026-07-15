# Platform Admin Panel — integration guide

One endpoint drives the whole panel: `GET /api/v1/admin/catalog` on the api-gateway. This guide
shows a front end how to consume it, and shows any service — ours or a customer's own product — how
to appear in it. The machine contract is [`admin-panel.openapi.yaml`](./admin-panel.openapi.yaml);
the per-action schema is [`admin-actions.schema.json`](./admin-actions.schema.json).

## The one principle

The panel is a **map, never the authority**. The catalog says what to *show*; every action is
re-authorized server-side at its own route when invoked. A front end never decides whether an
operator may do something — it only renders what the catalog returned and lets the service enforce.

## Front end: render and invoke

```js
// 1. One call, with the operator's own JWT (same bearer token used everywhere).
const catalog = await fetch("/api/v1/admin/catalog", {
  headers: { Authorization: `Bearer ${operatorJwt}` },
}).then(r => r.json());

// 2. Render. Each service block groups its actions; `group` gives UI sections.
for (const { service, actions } of catalog.services) {
  for (const a of actions) {
    renderControl({
      label: a.label,
      section: a.group,
      destructive: a.danger,          // style prominently
      confirmFirst: a.confirm,        // gate behind a confirmation step
      fields: a.params ?? [],         // build the form (name/in/type/required/enum/help)
    });
  }
}
for (const name of catalog.unreachable) markDegraded(name);  // answered? if not, show degraded

// 3. Invoke on the SAME gateway origin. Substitute {path} params; split query vs body.
async function invoke(a, values) {
  let path = a.path;
  const query = new URLSearchParams();
  const body = {};
  for (const p of a.params ?? []) {
    if (p.in === "path")  path = path.replace(`{${p.name}}`, encodeURIComponent(values[p.name]));
    if (p.in === "query" && values[p.name] != null) query.set(p.name, values[p.name]);
    if (p.in === "body"  && values[p.name] != null) body[p.name] = values[p.name];
  }
  const qs = query.toString();
  return fetch(qs ? `${path}?${qs}` : path, {
    method: a.method,
    headers: { Authorization: `Bearer ${operatorJwt}`, "Content-Type": "application/json" },
    body: ["GET", "DELETE"].includes(a.method) ? undefined : JSON.stringify(body),
  });
}
```

A `403` on invoke is normal and expected: the catalog is filtered to the caller's capabilities, but
capabilities can change between opening the panel and clicking, and the route is the source of truth.
Show the error; never assume the catalog guaranteed success.

## Any service: appear in the panel

A service opts in by declaring `app/admin_actions.py` and emitting it under `admin_actions` in its
`GET /internal/manifest`. That is the *entire* contract — a customer's product service written in any
language appears automatically the moment its manifest carries the same shape and the gateway's
`ADMIN_SERVICES` config points at it. Nothing about the panel is hardcoded to our 17 services.

```python
# app/admin_actions.py — a PURE literal (the safety:admin CI gate reads it with ast.literal_eval).
ADMIN_ACTIONS: list[dict] = [
    {"id": "billing.void_invoice", "label": "Void an invoice", "method": "POST",
     "path": "/api/v1/invoices/{id}/void", "capability": "platform.billing", "group": "Invoices",
     "danger": True, "confirm": True,
     "params": [{"name": "id", "in": "path", "type": "string", "required": True}]},
]
```

Rules the CI gate (`safety:admin`) enforces so the panel can never drift from the code:
- **R1** every declared action maps to a real route in the same manifest's `endpoints`.
- **R2** `path` is relative — no scheme, host, or protocol-relative `//host` (dropped as an open redirect).
- **R3** `method` is in the allow-list `GET/POST/PUT/PATCH/DELETE`.
- **R4** `id`, `capability`, and `label` are well-formed.
- **R5** the declared `capability` is advertised in the manifest's `capabilities`.

## What is deliberately NOT an action

- **Internal-token routes** (`/internal/...`) — service-to-service, driven by the platform, never an
  operator click. Money movement, indexing, queue claim/ack, delivery, scheduler ticks all live here.
- **Self-service routes** (a person acting on their OWN data — their inbox, their files) — tenant-scoped
  from the token, not an operator surface.
- Services with neither (file-service, api-gateway) declare an explicit empty list so the *absence*
  is a reviewed decision, not a forgotten file.

## Security properties (phase 33)

| Threat | Control |
|---|---|
| SSRF via discovery (T2) | Services to poll come from `ADMIN_SERVICES` config, never a request. |
| Token leak to browser (T3) | The internal token rides server-side only; the browser uses its own JWT. |
| Hostile declaration / open redirect (T4) | Declarations are untrusted input — method allow-listed, path forced relative, before anything is returned. |
| Control enumeration (T5) | The catalog is filtered to the caller's capabilities; opening it needs `ADMIN_CATALOG_CAP`. |
