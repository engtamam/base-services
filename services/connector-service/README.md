# connector-service

> Self-contained, **general & plug-and-play** third-party integration service (SaaS checklist
> §10): OAuth2/API-key connections with **encrypted credentials at rest**, **single-flight
> token refresh**, incremental **sync with a loss-proof watermark**, per-record idempotency,
> partial-failure results + DLQ, `Retry-After`-aware backoff, circuit breaker, **SSRF-guarded
> self-hosted URLs**, and per-connection health. Backend only. Zero new runtime deps.

---

## Providers are config, not code

The headline adapter is **`oauth2-rest`** — a spec-driven connector. Describe a provider once
and it works:

```json
{"token_url": "https://api.hubapi.com/oauth/v1/token",
 "base_url":  "https://api.hubapi.com",
 "auth_style": "body",                     // or "basic"
 "endpoints": {"contacts": {"path": "/crm/v3/objects/contacts",
                            "records_path": "results",
                            "cursor_param": "after",
                            "next_cursor_path": "paging.next.after"}},
 "field_map": {"email": {"op": "path", "value": "properties.email"}}}
```

Anything speaking OAuth2 + JSON REST becomes a spec entry. **Slack** and **HubSpot** ship as
concrete adapters (both verified against their official docs) to prove the shape; a vendor
with a bespoke protocol gets its own adapter behind the same port.

| Port | Drivers |
|------|---------|
| `ConnectorProvider` | **oauth2-rest** (spec-driven) · slack · hubspot · recording (no network) · null (fail-closed) |
| `Clock` | system · manual |

## Single-flight refresh is correctness, not performance

Many providers **rotate** the refresh token on every refresh. If two concurrent calls both
refresh, one ends up holding a token the provider has already invalidated and the connection
is **permanently broken**. So:

- credential resolution is **cached** (0 datastore reads on the hot path), and the cache entry
  carries its own expiry — a cache must never outlive the token it holds;
- refresh happens **early** (`TOKEN_REFRESH_SKEW_SECONDS`), because clocks disagree;
- refresh is **single-flight**: *N* concurrent expiring calls perform exactly **one** refresh
  (benchmark-asserted at N=25);
- the **rotated** refresh token is persisted *before* the access token is used; a provider that
  doesn't rotate (HubSpot) keeps its existing token rather than having it blanked;
- if a peer replica rotated first (`invalid_grant`), we re-read and adopt its fresh token
  instead of condemning the connection to re-auth.

`disconnect` destroys the sealed credentials and bumps a generation, so a cached token dies
**immediately**.

## Sync guarantees

- **The watermark advances only after the batch is durably processed.** Advancing it first —
  the obvious implementation — permanently skips every record of a batch that then fails.
  Silent, unrecoverable data loss. Here a *partial* run leaves the cursor where it was.
- **Per-record idempotency** (stable key from connection + resource + source id): a retried
  batch re-processes nothing it already committed, so at-least-once retries never double-write.
- **Partial failure is reported as partial** — 997 ok / 3 failed ⇒ the 997 stand, the 3 are
  **dead-lettered**, and the run is `partial`. Never "success"; never a rollback of good work.
- **Echo-loop guard** — records pulled from a provider are tagged with their origin and are
  never pushed back (A→B→A loops burn rate limits and corrupt data).
- **`Retry-After` is obeyed** when the provider sends it; otherwise full-jitter backoff.
- **Circuit breaker** per connection; **health** exposes error rate, last error, watermarks.

## Security

Credentials are **AES-256-GCM sealed at rest**, never returned by any API (`public()` strips
them), and **redacted from logs/errors** — a provider error never carries the token. Customer
**instance URLs are SSRF-validated and IP-pinned** (private/loopback/link-local and cloud
metadata blocked, even with `allow_private` on). Field mappings are declarative — **no eval**,
unknown ops fail closed. Deny-by-default + capability RBAC; unknown drivers fail fast.

## Endpoints

| Tier | Method · Path | Purpose |
|------|---------------|---------|
| internal | `POST /internal/connectors/sync` | run one batch (job-service schedules + retries) |
| user | `POST/GET /api/v1/connectors/specs` | provider specs (config, not code) |
| user | `POST /api/v1/connectors/connect` · `/{id}/disconnect` | OAuth2 connect / revoke |
| user | `POST /api/v1/connectors/sync` · `GET /runs` · `/dead-letters` | sync, history, DLQ |
| user | `GET /api/v1/connectors` · `/{id}/health` | connections + monitoring |

Caps: `platform.connectors` · `connectors.manage` · `connectors.read`.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
uvicorn app.main:app
pytest -q ; python ci/security_gate.py
```

## Out of scope
Vendor-specific object models (SAP IDoc, Salesforce SOQL) — the spec-driven connector covers
REST/OAuth2 · GUI mapping builder · vendor-risk questionnaire (compliance-service).
