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

## Plug-and-play (config only — no code changes)

| Choice | Env knob | Options |
|--------|----------|---------|
| Where users live | `DATASTORE_DRIVER` | `memory` · `postgres` |
| Password hashing | `PASSWORD_HASHER_DRIVER` | `argon2` · `bcrypt` |
| Token signing | `TOKEN_ISSUER_DRIVER` | `null`(deny) · `hs256` · `rs256` |
| MFA | `MFA_DRIVER` | `null`(off) · `totp` |
| Open signup | `REGISTRATION_ENABLED` | `true` · `false` |
| Access/refresh TTL | `ACCESS_TTL_SECONDS` / `REFRESH_TTL_SECONDS` | seconds |

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
