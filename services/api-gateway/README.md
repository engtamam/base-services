# api-gateway

> Self-contained, plug-and-play **FastAPI** edge gateway (SaaS checklist §10). The
> platform's single front door: **routes** each request to the owning upstream,
> **enforces auth at the edge** (JWT verify + capability check), **injects trusted
> identity headers**, **rate-limits**, and optionally **caches**. Backend only.
>
> **Not** nginx/Caddy — it runs *behind* them (they do TLS + L7 edge; this does
> app-aware auth/routing/header-injection). Built from `_template` (inherits ports,
> auth, CWE guard, CI gate). One new dep: `httpx` (BSD).

---

## How it works

```
client → [nginx/Caddy: TLS] → api-gateway → identity / notification / tenant / …
                                  match route · verify JWT · inject headers · rate-limit · cache
```

Routes are **data**. Add a service to the platform = add one route row — no code.

## Route config (`GATEWAY_ROUTES` JSON, or `GATEWAY_ROUTES_FILE`)

```json
[
  {"prefix": "/identity", "upstream": "http://identity-service:8001", "auth": "none",     "strip_prefix": true},
  {"prefix": "/notify",   "upstream": "http://notification-service:8002", "auth": "required", "capabilities": ["notify.send"]},
  {"prefix": "/public",   "upstream": "http://content:8010", "auth": "none", "cache_ttl": 30, "methods": ["GET"]}
]
```

| Field | Meaning |
|-------|---------|
| `prefix` | incoming path prefix (longest match wins) |
| `upstream` | fixed target base URL (**config only** — the request never sets the host) |
| `auth` | `none` · `optional` · `required` (verify JWT at the edge) |
| `capabilities` | required caps when `auth=required` → 403 otherwise |
| `strip_prefix` | strip `prefix` before forwarding |
| `methods` | allow-list (empty = all) |
| `cache_ttl` | seconds; cache safe GETs (non-`required`) |

## Trust model (the important bit)

On a verified request the gateway **injects**: `X-User-Id`, `X-Tenant-Id`, `X-Caps`,
`X-Request-Id`, and the shared `X-Internal-Token` (so upstreams trust these). It
**strips** any client-supplied copies of those headers — a caller can **never spoof**
identity. The original `Authorization` header is still forwarded so upstreams may
re-verify (defense in depth).

## Endpoints

| Tier | Path | Purpose |
|------|------|---------|
| public | `/healthz` `/readyz` `/metrics` | probes / metrics |
| internal | `/internal/routes` (token) | list the route table |
| proxy | `/{any}` | matched against routes → forwarded, else 404 |

## Plug-and-play (config only)

| Choice | Knob | Options |
|--------|------|---------|
| Routes | `GATEWAY_ROUTES` / `GATEWAY_ROUTES_FILE` | any JSON table |
| Forwarder | `FORWARDER_DRIVER` | `httpx` |
| Route source | `ROUTE_TABLE_DRIVER` | `static` (discovery later) |
| Auth verify | `TOKEN_VERIFIER_DRIVER` | `hs256` · `rs256` · `jwks` |
| Rate limit / cache | `RATELIMITER_DRIVER` / `CACHE_DRIVER` | memory · redis |
| Upstream timeout | `UPSTREAM_TIMEOUT_SECONDS` | int |

## Security

**SSRF-safe by construction** — the upstream host is config; the request only adds a
path (the URL authority can never change). Identity/internal-token **spoof stripping**;
hop-by-hop headers dropped; edge rate-limit (fail-closed); upstream timeout → 504.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
GATEWAY_ROUTES='[{"prefix":"/id","upstream":"http://localhost:8001","auth":"none"}]' uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope (later)

API-key issuance / OAuth2 client-creds · inbound/outbound webhooks · OpenAPI
aggregation / dev portal · dynamic service discovery (a future RouteTable adapter).
