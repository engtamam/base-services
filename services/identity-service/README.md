# identity-service

> Self-contained, plug-and-play **FastAPI** identity service. It **issues** the
> JWTs that every other base-services service **verifies**. Backend + endpoints
> only (no UI). Built from the `_template` seed (inherits ports, security headers,
> deny-by-default auth, CWE self-guard, and the CI security gate).

---

## What it does

Register users, log them in, issue short-lived **access** + long-lived **refresh**
tokens, rotate/revoke refresh tokens, expose the caller's profile, change
passwords, and (optionally) enforce **TOTP MFA**.

## Endpoints (`/api/v1/auth`)

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/register` | rate-limited | create a user (gated by `REGISTRATION_ENABLED`) |
| POST | `/login` | rate-limited | verify credentials → access + refresh tokens |
| POST | `/refresh` | refresh token | **rotate** → new tokens (old refresh revoked) |
| POST | `/logout` | refresh token | revoke a refresh token |
| GET | `/me` | access token | the caller's profile (never leaks secrets) |
| POST | `/change-password` | access token | change own password |
| POST | `/mfa/enroll` | access token | provision TOTP (when `MFA_DRIVER=totp`) |
| POST | `/mfa/verify` | access token | verify a TOTP code |

Plus the inherited tiers: `/healthz` `/readyz` `/metrics` (public), `/internal/*`
(token), `/internal/manifest` (lists ports + endpoints).

### Multi-tenant membership (`IDENTITY_MULTI_TENANT=true`)

One person, many companies — a freelancer with two clients gets **one login** and a key to each
door, and can only ever stand in one room at a time (a token carries **exactly one tenant**). Off
by default; when off these routes 404.

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| POST | `/api/v1/auth/person/login` | rate-limited | authenticate the person → their list of tenants (**no token yet**) |
| POST | `/api/v1/auth/tenant` | person creds or a live access token | mint a token scoped to **one** chosen tenant |
| POST | `/api/v1/auth/person/claim` | one-time invite | an invited person sets their **own** password |
| GET | `/api/v1/tenancy/members` | `members:read` cap | who works in **your** tenant (tenant off the token) |
| POST | `/api/v1/tenancy/members` | `members:write` cap | add a member (invite by default; never touches an existing person's credentials) |
| DELETE | `/api/v1/tenancy/members/{id}` | `members:write` cap | revoke a membership (the person survives — they work elsewhere) |
| GET | `/internal/tenancy/members?tenant=` | internal token | the directory for a **background job** (no user JWT) |

**Capabilities from roles.** With `IDENTITY_CAPS_SOURCE=authz`, a member's caps are resolved from
authz-service at mint time (`roles` are the input, `caps` are derived) — edit a role, everyone's
caps follow on next mint. Default `membership` keeps caps on the row. Authz unreachable → **no
token** (fails closed), never a token with zero caps.

## Plug-and-play (config only — no code changes)

| Choice | Env knob | Options |
|--------|----------|---------|
| Where users live | `DATASTORE_DRIVER` | `memory` · `postgres` |
| Password hashing | `PASSWORD_HASHER_DRIVER` | `argon2` · `bcrypt` |
| Token signing | `TOKEN_ISSUER_DRIVER` | `null`(deny) · `hs256` · `rs256` |
| MFA | `MFA_DRIVER` | `null`(off) · `totp` |
| Open signup | `REGISTRATION_ENABLED` | `true` · `false` |
| Access/refresh TTL | `ACCESS_TTL_SECONDS` / `REFRESH_TTL_SECONDS` | seconds |
| Multi-tenant | `IDENTITY_MULTI_TENANT` | `false`(default) · `true` |
| Where caps come from | `IDENTITY_CAPS_SOURCE` | `membership` · `authz` |
| How a new member is added | `IDENTITY_MEMBER_ADD_MODE` | `invite`(default, secure) · `password` |
| Step-up on tenant switch | `IDENTITY_TENANT_SWITCH_REAUTH` | `false` · `true` |

## How other services verify its tokens

identity **signs** with its `TokenIssuer` key; peers **verify** with the matching
`TokenVerifier`:
- **hs256** — peers set `TOKEN_VERIFIER_DRIVER=hs256` + the same `TOKEN_HS256_SECRET`.
- **rs256** — identity holds `TOKEN_PRIVATE_KEY`; peers hold only `TOKEN_PUBLIC_KEY`
  (verify-only, cannot forge). Preferred for zero-trust.

## Security

argon2 hashing · **no user enumeration** (uniform response) · refresh **rotation +
revocation** (replay rejected) · login rate-limiting · pinned-alg token verify ·
`/me` wrapped in the CWE respond-guard so `password_hash`/`mfa_secret` can never
leak · safe generic errors.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
# .env already has a random INTERNAL_SERVICE_TOKEN + a shared hs256 secret
uvicorn app.main:app
pytest -q            # tests
python ci/security_gate.py   # security gate
```

## Out of scope (later)

OAuth2 / SAML / passwordless / magic-link · JWKS publication for multi-service key
distribution · account-recovery emails (needs notification-service).

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
