# base-services

Twenty production-grade backend microservices in Python / FastAPI, plus the Helm chart and
CI that deploy them. Every service is **self-contained**: no shared runtime library, no
cross-service import, one copy-me template. Hexagonal, deny-by-default, zero-hardcoded.

> **Proprietary. All rights reserved.** See [`LICENSE`](LICENSE) — no licence is granted to
> anyone. Keep this repository **private**.

---

## The surface you adopt

Every file below is a **pure literal or plain config** — readable, and checkable, without running
the service. If you are fitting one of these to your platform, these are the files to read first;
everything else is implementation you should not need to touch.

| File | What it decides |
| --- | --- |
| `app/config.py` | **Every knob.** No URL, limit, path or vocabulary is hardcoded anywhere else. Read this before deploying anything. |
| `app/manifest.py` | The routes and capabilities this service actually serves — the source of truth the gates check declarations against. |
| `app/ports/*.py` | The contracts. Swap the matching `app/adapters/*/` to run on your infrastructure instead of ours. |
| `app/adapters/*/` | The drivers behind each port (`memory`, `postgres`, `redis`, `s3`, `http`, `null`…). Chosen by config, never by code. |
| `app/admin_actions.py` | What an operator panel may show — and the capability each action needs. |
| `app/user_actions.py` | The self-service catalog, plus `UNDECLARED_ROUTES` with a stated reason for anything deliberately absent. |
| `app/auditable_actions.py` | What is recorded, and which actions **fail closed** when the audit log is unreachable. A gate enforces that every declared action is actually recorded — not merely the critical ones — so the contract cannot claim a control the code never writes. |
| `app/service_contracts.py` | What this service CALLS in another — the other half of the seam. **Absent means it calls nothing**, which is most services. |
| `deploy/services/<svc>.values.yaml` | Deployment shape. Images are digest-pinned at release; never a `:latest` tag. |
| `docs/openapi/<svc>.openapi.json` | The HTTP surface, generated from the running app rather than written by hand. |

A service you are not using can be deleted outright — nothing imports across service boundaries.

## The services

| | | |
| --- | --- | --- |
| **identity** — auth, tokens, lockout, refresh-reuse detection | **tenant** — multi-tenancy | **authz** — capability RBAC, policy decisions |
| **billing** — subscriptions, invoices, 12 gateways, entitlements, off-gateway settlement | **wallet** — prepaid balances, holds, ledger *(optional)* | **zatca** — KSA e-invoicing |
| **notification** — email/SMS/push, KSA providers | **webhook** — outbound delivery, SSRF-guarded | **connector** — third-party integrations, OAuth2 |
| **analytics** — events, rollups, funnels, retention | **search** — Arabic-aware full-text | **feature-flag** — rules, rollout, entitlement-aware |
| **job** — queue, leases, backoff, dead-letter | **audit** — hash-chained, retention | **compliance** — PDPL + GDPR, DSAR, consent |
| **file** — object storage | **api-gateway** — the only public door | **verification** — Nafath, CR, VAT claims |
| **onboarding** — requirements, not a wizard | **`_template`** — the copy-me seed | |

Each has its own `README.md`, its own tests, its own security gate, and its own venv-free
`pyproject.toml`. Delete any one of them and the rest still build.

## What makes them different

Every service answers four questions before a line of code is written — hot path, config knobs,
what production needs that the checklist omits, and the correctness footgun. The answers live in
[`docs/phases/`](docs/phases). They produced things you would not get from a checklist:

- **You cannot sum uniques, and you cannot average percentiles.** analytics stores mergeable
  HyperLogLog and histogram sketches, so a weekly unique count is a real union and a daily p95
  is a real percentile.
- **A usage counter cannot be cached.** billing's limits are cached by generation; its usage is
  read-and-incremented atomically, every time, because a cached counter lets two pods both
  approve the 26th seat.
- **A bank reference is unique per bank, not per planet.** A payment's identity is
  `(method, issuer, reference)`.
- **A gauge is not a counter.** Increment `seats` like `api_calls` and deleting a user never
  gives the seat back.
- **`/healthz` touches no dependency.** Point liveness at `/readyz` and one slow database moment
  restarts the entire fleet.

Two guard tests live in every service and have caught real defects in most of them: a **hot-path
benchmark** (zero reads/writes where we claim zero) and a **config-knob guard** (every setting
has a consumer — no knob ships before its code).

## Quick start

```bash
cd services/wallet-service
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev,ci]"
python -m pytest -q                       # 195 tests (10 more with a database)
python ci/security_gate.py                # licenses / SAST / secrets / CVEs
uvicorn app.main:app --port 8017
```

Create a new service — it arrives with the guards, the gates and a deploy values file already
wired:

```bash
python scripts/create_service.py my-service --desc "..."
```

## Deploying

One generic chart for all seventeen. It **refuses to render a mistake**: scaling out on
in-memory drivers, an HPA without CPU requests, a PodDisruptionBudget on a singleton,
`/internal` exposed to the internet, an unpinned image, a values key nothing reads.

```bash
cd deploy
make up        # K3s + Calico + metrics-server + postgres/redis
make verify    # PROVES the cluster enforces what the chart promises
make deploy    # all 17 services
```

`make deploy` depends on `make verify`. Bootstrap *installs*; preflight *proves* — it sends a
packet that a default-deny NetworkPolicy must refuse, because checking that Calico is present
proves it was installed, not that it blocks anything.

**Egress is deny-by-default.** Each allow is opt-in per service: `dns`, `datastore` (postgres/redis
by label), `internet` (third-party callers, SSRF-guarded), `objectStore` (an in-cluster S3 on the
S3 port), and an `extra` list to declare anything else verbatim. A dependency a service cannot reach
because no allow opens it fails loudly now — `/readyz` bounds each probe and reports
`{"<dep>": "timeout"}` rather than hanging with the pod stuck `Running 0/1`.

**Object store (optional, plug-and-play).** The blob services (`file`, `backup`, `stream`) default
to a local driver. Point them at S3 with `OBJECTSTORE_DRIVER=s3` + `OBJECTSTORE_ENDPOINT_URL` /
`_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET` — your own MinIO, on-prem, or cloud S3. Or run the bundled
reference (`OBJECTSTORE_ENABLED=1 deploy/k3s/bootstrap.sh`): **SeaweedFS** (Apache-2.0 — *not* MinIO,
whose server is AGPLv3 and outside this platform's permissive-only licence policy). The chart's
`objectStore` egress opens to it automatically.

See [`deploy/README.md`](deploy/README.md).

## Gates

One script is the single source of truth. CI installs the infra and calls it. **`local == CI`.**

```bash
tools/ci/run_all_gates.sh --list        # what each gate protects
tools/ci/run_all_gates.sh --profile quick
tools/ci/run_all_gates.sh               # everything runnable here
```

Nine gates, each blocking a merge. Three outcomes, never two: `PASS`, `FAIL`, or a loud
`SKIP (WIRED)`. CI runs `--strict`, where a skipped gate **fails** — so "we installed the
database" is something CI proves, not something a comment asserts.

Two of them are meta-gates, because the gates need guarding too:

- **`safety:ci-parity`** — the workflow and the script must agree in both directions, and every
  workflow must be valid YAML. (GitHub skips an unparseable workflow *silently*.)
- **`coverage:services`** — every service carries the probe contract, hot-path benchmark, knob
  guard and deploy values. Exceptions live in a **shrinking** allowlist; a stale entry fails.

The atomic primitives (`incr_if_below`, `cas`) are tested against a **real PostgreSQL**, because
the in-memory adapter's lock hides every race a database has to serialise itself. That run has
found bugs the memory adapter never could.

## Releasing

Tag `v*`. The pipeline runs the gates, builds each service to GHCR **by digest** with signed
build provenance, Trivy-scans the published image, pins those digests into the values files,
proves the chart renders with them, and attaches the result to a GitHub Release.

A release is not "some images were pushed" — it is *these exact digests, and the chart renders
with them*. See [`RELEASING.md`](RELEASING.md).

## Layout

```
services/           17 services + _template, each fully self-contained
deploy/             one Helm chart, per-service values, K3s bootstrap + preflight
tools/ci/           run_all_gates.sh — the merge-blocking gates
tools/release/      digest pinning
tests/deploy/       chart guards (no cluster needed)
docs/phases/        why each service is built the way it is
```

## Notes

- Python **3.12**. Backend and endpoints only; no UI.
- Dependencies must be permissively licensed (MIT / BSD / Apache-2.0 / ISC / PSF). Enforced in
  CI. This constraint governs what the project *consumes*; the project itself is proprietary.
- Built for the KSA market where it matters (ZATCA, Mada, Unifonic, Taqnyat, PDPL) — and generic
  everywhere it doesn't. No project's feature names appear in any service.
