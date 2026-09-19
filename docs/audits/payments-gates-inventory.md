# Platform-wide gate inventory — vulnerability classes from the checkout audit

**Date:** 2026-09-19 · **Mode:** advisory (detectors built, NOT yet registered as blocking).
Each class found in the payments checkout audit is now a precise, AST-based, fleet-wide detector in
`tools/ci/`, run over every service's `app/`. This is the inventory to decide what to remediate.
Every detector supports an in-line opt-out marker for a genuinely-acceptable site.

| Class (audit id) | Detector | Findings | Services | Precision | Ready to enforce? |
|---|---|---:|---:|---|---|
| AES-GCM without AAD (M1) | `check_aesgcm_aad.py` | 85 | 23 | high | after fleet remediation |
| cas-updated row created via put() (C1 / identity incident) | `check_cas_create.py` | 11 | 11 | high (triage) | after triage+fix |
| money as float / hardcoded exponent (L6) | `check_money_float.py` | 14 | 2 | high | after fix |
| non-constant-time secret compare (L2) | `check_timing_compare.py` | 3 | 2 | high | after fix |
| open redirect to non-literal (M5) | `check_open_redirect.py` | 3 | 2 | medium (triage) | after triage |
| SSRF egress (H1) | `check_ssrf_egress.py` | 0* | 0 | low* | needs rework |
| swallowed broad except (M8) | `check_swallowed_except.py` | 650 | 25 | low (noisy) | needs tuning |

## Per-class notes

### ⭐ cas-create (11) — the highest-value class; same shape that 401'd every identity refresh
Candidate rows that are version-pinned `cas()`-updated but created via `put()` (so on postgres the
update never matches): `analytics _ROLLUPS`, `billing (self.collection)`, `feature-flag _FLAGS`,
`job _JOBS`, `stream _SESSIONS`, `tenant _TENANTS`, `ticket (self.collection)`,
`verification _VERIFICATIONS`, `wallet _IDEMPO`, `webhook _DELIVERIES`, and one more. **Each needs
triage** — some may create via cas elsewhere or write an already-versioned row; the real ones are
latent production bugs of the identity-refresh class and should be fixed (create via
`cas(C, k, 0, …)`), each with a regression test, then the gate registered blocking.

### ⭐ money-float (14) — precise
`float(amount_minor)` / hardcoded `/100` `*100` in `billing` payment adapters (geidea, paytabs, tap,
tuwaiqpay) and `billing_service`. Fix by routing through an ISO-4217 minor-unit helper (the one
`payments-service/app/core/currency.py` now has). Then register blocking.

### aesgcm-aad (85 across 23) — precise but broad (copied infra)
Mostly `backup.py` (46 — whole-archive sealing; AAD may bind the archive/manifest id, or be marked
`# aad: ok` if genuinely N/A) and `secret_store.py` (28 — the real M1 class, fix like payments did),
plus `connector seal.py`, `assistant conversations.py`, `flow_crypto.py`, `hyperpay.py`. A fleet
remediation (thread `(collection|tenant|name)` AAD through the copied crypto). Decide backup.py policy
first.

### timing-compare (3) — precise
`file-service signed_url/hmac_signer.py` compares an HMAC with `!=` (a real timing side channel — use
`hmac.compare_digest`). The identity `password_hash` compares are sentinel checks (now filtered as
ALL-CAPS constants). Fix hmac_signer, register blocking.

### open-redirect (3) — triage
`billing pay-link` redirect and `file-service` download redirects (to object-store presigned URLs).
Likely have their own allowlist/are to a trusted store; confirm and mark `# redirect: ok` or add a
guard.

### SSRF (0*) — detector needs rework
Returned 0 because every raw `urlopen`/`requests` sink is wrapped in a `_send`/`post_json` helper,
which the detector's guard-heuristic treats as guarded. The real question — "does the URL's host come
from a tenant credential without an IP-range guard?" — needs data-flow, not a call-site heuristic.
Recommend: instead enforce that every outbound-HTTP helper (`*_http.py`) calls a shared egress guard
(payments-service already has `guard_fetch_url`); make that a small targeted gate rather than a
call-site scan.

### swallowed-except (650) — too noisy to enforce
The platform legitimately uses broad best-effort catches widely; the detector also misses `log.info`
-style logging (receiver named `log`, method not in the hint list). As-is this is noise. Options:
tune (recognize `logger.*`/`log.*` receivers; scope to routes/handlers only) or drop in favour of the
existing `defensive-access-hides-bugs` discipline.

## Recommendation
Enforce, in priority order, the precise high-value gates after their fleet remediation:
**cas-create → money-float → timing-compare → aesgcm-aad**. Triage **open-redirect**. Rework
**SSRF** into a helper-level guard gate. Defer/re-tune **swallowed-except**. Register each in
`tools/ci/gates.d/` (+ selftest, + ci-parity) once its fleet violations are burned down, so it fails
a merge that reintroduces the class.
</content>
