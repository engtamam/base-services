# Phase 6 — identity-service (detailed steps)

> **Goal:** the first real Tier-1 service — **identity-service** — built by cloning
> the hardened `_template` and adding auth business logic. It **issues** the JWTs
> that every other service **verifies**. Fully **plug-and-play**: the host SaaS
> chooses (by config) where users live, how passwords are hashed, how tokens are
> signed, and whether MFA is on — no code changes.
>
> _Reference: `docs/architecture-plan.md` §5 (Tier-1), §3 (hexagonal). Memory: self-contained · zero-hardcoded · ports-and-adapters · production-ready · permissive-licenses · CWE-guard._

---

## 0. Definition of done (Phase 6)

- [ ] `services/identity-service` generated from `_template` (self-contained; inherits ports, auth, CWE guard, CI gates).
- [ ] **Pluggable by config:** `PasswordHasher` (argon2/bcrypt), `TokenIssuer` (hs256/rs256), `MfaProvider` (null/totp), user storage via the existing `DataStore` port (memory/postgres). No hardcoded choices.
- [ ] Endpoints work: `register`, `login`, `refresh`, `logout`, `me`, `change-password` (+ MFA enroll/verify when enabled).
- [ ] **Security:** argon2 hashing; **no user enumeration** (uniform response/timing); refresh-token **rotation + revocation** (replay rejected); login rate-limited; **never** leaks `password_hash`/`mfa_secret` (CWE guard `respond`).
- [ ] Tokens it issues **verify** via the template `TokenVerifier` (matched by config).
- [ ] Its **own venv + deps** (argon2-cffi, pyotp — permissive); `pytest` + `ci/security_gate.py` **green**; live boot proven (register → login → me).
- [ ] Service README documents endpoints, config knobs, and the plug-and-play adapters.

---

## 1. Prerequisites & decisions

1. **New runtime deps (permissive):** `argon2-cffi` (MIT), `pyotp` (MIT); optional `bcrypt` (Apache-2.0) as a `[bcrypt]` extra. identity needs its **own venv** (template venv lacks these).
2. **User store = the generic `DataStore` port** (no new storage adapter). A `UserRepository` keeps a `username→id` index in the same store, so it works on memory today and postgres by config.
3. **identity is the issuer.** It holds the **signing** key (`TokenIssuer`); other services hold only the verify key (`TokenVerifier`). For a self-contained identity, both are set to the matching key by config.
4. **Scope:** core auth + refresh rotation + optional TOTP MFA. OAuth/SAML/passwordless are a later extension (documented, out of scope here).

---

## 2. Steps

### Step 6.1 — Generate the skeleton + own venv
- `python scripts/create_service.py identity-service --desc "Identity, auth & tokens" --token-verifier hs256 --no-verify` (verify later with its own venv).
- Add deps to `pyproject.toml`: `argon2-cffi`, `pyotp`; `[bcrypt]` extra.
- Create `.venv` (py -3.12), `pip install -e ".[dev,ci]"`.
- **Check:** installs clean; inherited 127 tests + gate still green.

### Step 6.2 — Config (zero-hardcoded)
- Add identity knobs to `app/config.py`: `PASSWORD_HASHER_DRIVER` (argon2), `TOKEN_ISSUER_DRIVER` (hs256/rs256), `TOKEN_SIGNING_SECRET`/`TOKEN_PRIVATE_KEY`, `ACCESS_TTL_SECONDS` (900), `REFRESH_TTL_SECONDS` (604800), `MFA_DRIVER` (null/totp), `MFA_ISSUER_NAME`, `REGISTRATION_ENABLED` (bool). Update `.env.example`.
- **Check:** boots with defaults (argon2, null MFA); fail-fast on bad values.

### Step 6.3 — Ports (interfaces)
- `app/ports/password_hasher.py` (`hash`, `verify`, `needs_rehash`), `app/ports/token_issuer.py` (`issue_access`, `issue_refresh`), `app/ports/mfa_provider.py` (`provision`, `verify`, `enabled`).
- **Check:** import pulls no third-party driver.

### Step 6.4 — Adapters + registry wiring
- Hashers: `argon2.py`, `bcrypt.py` (lazy extra). Issuers: `hs256.py`, `rs256.py` (PyJWT). MFA: `null.py` (disabled, deny), `totp.py` (pyotp).
- Extend `app/container.py` with 3 new registries (allowlist, fail-fast) + fields; `describe()`/manifest include them.
- **Check:** unknown driver → ConfigError; container builds with defaults.

### Step 6.5 — Domain: user model + repositories
- `app/core/models.py` `User` (id, tenant, username, password_hash, caps, roles, status, mfa_secret, timestamps).
- `app/core/user_repository.py` over `DataStore` (by id + username index; tenant-scoped). `app/core/refresh_store.py` (jti → {user_id, exp, revoked}) for rotation/revocation.
- **Check:** create/get/by-username/uniqueness; refresh store issue/rotate/revoke.

### Step 6.6 — Core auth logic (no framework)
- `app/core/auth_service.py`: `register`, `authenticate` (timing-safe, **no enumeration**), `issue_tokens`, `refresh` (verify → **rotate** old jti → new pair), `logout` (revoke), `change_password`.
- **Check:** unit tests cover each flow incl. rotation + revocation.

### Step 6.7 — Routes
- `app/api/v1/auth.py`: `POST /api/v1/auth/{register,login,refresh,logout,change-password}`, `GET /api/v1/auth/me`; MFA `POST /api/v1/auth/mfa/{enroll,verify}` (when enabled).
- Wire `rate_limit` on login/register, `require_user` on me/change-password/logout, and `guard.step("respond")` to block `password_hash`/`mfa_secret` leakage.
- `register` gated by `REGISTRATION_ENABLED` (else admin-cap).
- **Check:** routes mount; deny-by-default holds.

### Step 6.8 — Manifest & issuer/verifier match
- Manifest `capabilities` + `endpoints`; ensure `.env` sets `TOKEN_VERIFIER_DRIVER` to match the issuer (same secret/key) so `/me` verifies issued tokens.
- **Check:** `/internal/manifest` lists identity ports + endpoints.

### Step 6.9 — Tests
- Unit: hasher (hash≠plaintext, verify, needs_rehash), issuer (access/refresh verify), user repo, auth flows.
- Integration: register → login → me; refresh rotates (old refresh rejected); logout revokes; duplicate register fails; bad login 401.
- Security: **no enumeration** (unknown user vs bad password indistinguishable), `/me` + `login` never return `password_hash`/`mfa_secret`, refresh **replay** rejected, expired token 401.
- **Check:** all green.

### Step 6.10 — Verify (own venv) + live boot
- `pytest` + `python ci/security_gate.py` in the service → green.
- Live boot: register a user → login (get tokens) → call `/me` with the access token → 200 with profile (no secrets).
- **Check:** live flow works end-to-end.

### Step 6.11 — Docs
- Service `README.md`: what it is, endpoint table, config knobs, the plug-and-play adapter matrix (hasher/issuer/mfa/store), how another service verifies its tokens.
- Update `docs/architecture-plan.md` (identity ✅) + build-progress memory.
- **Check:** README complete.

---

## 3. Out of scope (deferred)

- OAuth2 / SAML / passwordless / magic-link → identity **extension** phase.
- Central multi-service key distribution (JWKS endpoint) → pairs with `jwks` verifier later.
- Account recovery emails → needs notification-service (Tier 1, next).

---

## 4. Execution checklist

| Task | Step | Status |
|---|---|---|
| #54 | 6.1 Skeleton + own venv | ✅ completed |
| #55 | 6.2 Config | ✅ completed |
| #56 | 6.3 Ports | ✅ completed |
| #57 | 6.4 Adapters + registry | ✅ completed |
| #58 | 6.5 Domain models + repos | ✅ completed |
| #59 | 6.6 Core auth logic | ✅ completed |
| #60 | 6.7 Routes | ✅ completed |
| #61 | 6.8 Manifest + issuer match | ✅ completed |
| #62 | 6.9 Tests | ✅ completed |
| #63 | 6.10 Verify + live boot | ✅ completed |
| #64 | 6.11 Docs | ✅ completed |
| —   | DoD (§0) all checked | ✅ completed (138 tests; gate PASS; live boot OK) |

_Legend: ⬜ pending · 🟡 in-progress · ✅ completed._

---

## 5. Why this is plug-and-play (the point)

The host SaaS wires identity to its stack **by config only**:

| Choice | Knob | Options |
|---|---|---|
| Where users live | `DATASTORE_DRIVER` | memory · postgres |
| Password hashing | `PASSWORD_HASHER_DRIVER` | argon2 · bcrypt |
| Token signing | `TOKEN_ISSUER_DRIVER` | hs256 · rs256 |
| MFA | `MFA_DRIVER` | null · totp |
| Open signup | `REGISTRATION_ENABLED` | true · false |

Same code, any SaaS. Other services just verify its tokens — no coupling.

_When every box is ✅, identity-service is live. Next Tier-1: notification-service (for recovery/OTP), then tenant, authz, gateway, file, audit._
