# Phase 9 — api-gateway (detailed steps)

> **Goal:** the Tier-1 **api-gateway** (SaaS checklist §10) — the platform's single
> front door. It **routes** each request to the owning upstream service, **enforces
> auth at the edge** (JWT verify + capability check) and **injects trusted identity
> headers** to upstreams, **rate-limits** and optionally **caches** — the
> "gateway-is-the-edge" pattern, done better. Plug-and-play: routes are **config**,
> the forwarder and route source are **swappable ports**, and it is **SSRF-safe by
> construction** (upstreams come only from config, never the request).
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1) · `docs/saas-checklist.md` §10._
> _Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive · CWE-guard · backend-only._

---

## 0. Definition of done (Phase 9)

- [ ] `services/api-gateway` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gate).
- [ ] **Config-driven routes:** a `RouteTable` port (static-from-config default) maps a path prefix → `{upstream, auth, strip_prefix, methods, capabilities, cache_ttl}`. An `HttpForwarder` port (httpx) proxies to the upstream.
- [ ] **Edge auth:** per-route `none | optional | required`; `required` verifies the JWT via the existing `TokenVerifier` (+ optional capability check) and returns 401/403 before forwarding.
- [ ] **Trusted header injection:** on a verified request the gateway injects `X-User-Id`, `X-Tenant-Id`, `X-Caps`, `X-Request-Id`, and the upstream `X-Internal-Token`; it **strips** any client-supplied copies of those (anti-spoofing).
- [ ] **Rate limiting** at the edge (per subject/IP) via the existing `RateLimiter` → 429 with headers.
- [ ] **Response cache** for cacheable GETs (`cache_ttl > 0`, `auth != required`) via the existing `Cache` port.
- [ ] **SSRF-safe:** the upstream host comes only from the route table (config); the forward URL is built safely (no host injection from the request path).
- [ ] **Longest-prefix routing**, method filtering, hop-by-hop header stripping, upstream timeout → 504.
- [ ] **Zero-hardcoded**; `pytest` + `ci/security_gate.py` green; live boot proven (fake upstream routed through the gateway with header injection).

---

## 1. Prerequisites & decisions

1. **One new runtime dep:** `httpx` (BSD-3-Clause, permissive) for async forwarding — already present in the template venv (dev), so verification reuses it; declared as a **runtime** dep in the gateway's pyproject (self-contained).
2. **Gateway is the edge, upstreams trust it.** Upstreams accept injected identity headers *because* the request carries the shared `X-Internal-Token` that only the gateway adds. The gateway **strips** client attempts to set identity/internal headers.
3. **Routes are data.** A JSON/TOML routes file (`GATEWAY_ROUTES_FILE`) or inline env JSON (`GATEWAY_ROUTES`) — zero-hardcoded, hot-swappable per deploy.
4. **SSRF by construction.** The request never supplies a host; only a path that is appended to a config upstream. Path is sanitized (no `..`, no `@`, no scheme).

---

## 2. Steps

### Step 9.1 — Generate skeleton + httpx dep
- `python scripts/create_service.py api-gateway --desc "Edge gateway: routing, auth, rate-limit"`.
- Add `httpx` to `pyproject` runtime deps. Reuse the template venv for verification (has httpx).
- **Check:** inherited tests + gate green.

### Step 9.2 — Config (zero-hardcoded)
- `GATEWAY_ROUTES` (inline JSON) / `GATEWAY_ROUTES_FILE` (path); `UPSTREAM_TIMEOUT_SECONDS` (15); `FORWARDER_DRIVER` (httpx); `ROUTE_TABLE_DRIVER` (static); `DEFAULT_CACHE_TTL` (0). Update `.env.example`.
- **Check:** boots with an empty/default route table; fail-fast on malformed routes JSON.

### Step 9.3 — Ports
- `app/ports/route_table.py` (`match(path) -> Route|None`, `all() -> list[Route]`), `app/ports/http_forwarder.py` (`forward(method, url, headers, body, params) -> (status, headers, body)`).
- **Check:** import pulls no third-party driver (httpx imported lazily in the adapter).

### Step 9.4 — Adapters + registry
- `RouteTable`: `static.py` (parse routes from config JSON/file into `Route` objects, longest-prefix index). `HttpForwarder`: `httpx_forwarder.py` (lazy `httpx`, async, timeout, hop-by-hop stripping).
- Extend `container.py` with `route_table`, `forwarder` (allowlist, fail-fast).
- **Check:** unknown driver → ConfigError; routes parse; malformed route → ConfigError.

### Step 9.5 — Route model + matching
- `core/models.py` `Route` (prefix, upstream, auth[none|optional|required], strip_prefix, methods, capabilities, cache_ttl). Longest-prefix match; method allow-list; safe upstream-URL build (`_build_url`).
- **Check:** longest-prefix wins; disallowed method → None/405; path sanitization rejects `..`/`@`/scheme.

### Step 9.6 — Core Gateway
- `core/gateway.py`: `handle(request)` → match route (404 if none) → method check (405) → **rate-limit** (429) → **auth** (verify/caps → 401/403; build identity headers) → **cache get** (hit → return) → **forward** (inject headers, strip client identity/internal + hop-by-hop) → **cache set** → return. Upstream timeout/conn error → 504.
- **Check:** unit tests for each branch with a fake forwarder + fake verifier.

### Step 9.7 — Routes (proxy + admin)
- A catch-all proxy handler mounted **last**: `@router.api_route("/{full_path:path}", methods=[...])` → `gateway.handle`.
- Internal: `GET /internal/routes` (list the route table). Gateway's own `/healthz`/`/metrics`/`/internal/*` take precedence over the catch-all.
- **Check:** unmatched path → 404; own endpoints not shadowed.

### Step 9.8 — Manifest & wiring
- Manifest: ports (route_table/forwarder drivers) + a summary of configured route prefixes; endpoints.
- **Check:** `/internal/manifest` + `/internal/routes` list the routes.

### Step 9.9 — Tests
- Unit: longest-prefix match, method filter, `_build_url` safety (no host injection), auth enforce (none/optional/required + caps), header injection + **client spoof stripping**, rate-limit 429, cache hit/miss, upstream error → 504 (fake forwarder).
- Integration: run a **fake upstream** ASGI app in-process; route a request through the gateway → asserts the upstream saw injected `X-User-Id`/`X-Internal-Token` and **not** a client-spoofed one; public route works without a token; protected route 401 without token.
- Security: SSRF (a route's upstream is fixed; a crafted path can't change host), internal-token/identity spoof stripped, deny-by-default on required routes.
- **Check:** all green.

### Step 9.10 — Verify + live boot
- `pytest` + `ci/security_gate.py` green.
- Live boot: start a tiny fake upstream on a port; configure a route `/echo → that upstream`; `GET /echo/hello` through the gateway returns the upstream response and the upstream received the injected identity headers.
- **Check:** end-to-end works.

### Step 9.11 — Docs
- Service `README.md`: what it is, route config format, header-injection/trust model, plug-and-play matrix, SSRF stance.
- Update `docs/architecture-plan.md` (api-gateway ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- **API key issuance / OAuth2 client-credentials** → identity extension or a keys service.
- **Inbound/outbound webhooks** → webhook-service (Tier 2).
- **Developer portal / OpenAPI aggregation** → later.
- **Dynamic service discovery** (Consul/K8s) → a future `RouteTable` adapter.

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #87 | 9.1 Skeleton + httpx | ✅ completed |
| #88 | 9.2 Config | ✅ completed |
| #89 | 9.3 Ports | ✅ completed |
| #90 | 9.4 Adapters + registry | ✅ completed |
| #91 | 9.5 Route model + matching | ✅ completed |
| #92 | 9.6 Core Gateway | ✅ completed |
| #93 | 9.7 Routes (proxy + admin) | ✅ completed |
| #94 | 9.8 Manifest & wiring | ✅ completed |
| #95 | 9.9 Tests | ✅ completed |
| #96 | 9.10 Verify + live boot | ✅ completed |
| #97 | 9.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (136 tests; gate PASS; live boot OK) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play (the point)

| Choice | Knob | Options |
|---|---|---|
| Routes | `GATEWAY_ROUTES` / `GATEWAY_ROUTES_FILE` | any JSON route table |
| Forwarder | `FORWARDER_DRIVER` | httpx (add aiohttp/etc. later) |
| Route source | `ROUTE_TABLE_DRIVER` | static (add discovery later) |
| Auth verify | `TOKEN_VERIFIER_DRIVER` | hs256 · rs256 · jwks (shared with peers) |
| Rate limit / cache | `RATELIMITER_DRIVER` / `CACHE_DRIVER` | memory · redis |

Add a service to the platform = add one route row. The gateway verifies once at the
edge and upstreams trust its injected headers — no per-service auth wiring.

_When every box is ✅, api-gateway is live. Next Tier-1: authz · file · audit._
