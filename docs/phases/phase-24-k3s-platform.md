# Phase 24 — K3s platform (plug-and-play deployment)

> One generic Helm chart + a K3s bootstrap. Every service deploys with a ~20-line `values.yaml`.
> Nothing is hand-written per service; nothing is hardcoded; nothing claims a guarantee it
> cannot enforce.

Studied: `Security-tools` (kobo) `channels/cloud_api/k8s`, `kobo/installer/k8s`. Its cloud stack
is sound; what we add is *enforcement of the things it gets right*, so they cannot be forgotten
by the next person who writes a values file (see §5).

---

## 1. Definition of Done — answered BEFORE any YAML

### (1) Hot path

The chart renders at deploy time, so there is no request hot path *inside* it. There are two
hot paths it **controls**, and both are proven, not asserted:

* **The probe path.** `livenessProbe` fires on every pod forever. Pointing it at `/readyz` —
  which touches Postgres/Redis — turns one slow DB moment into a fleet-wide restart storm.
  * **Benchmark:** `/healthz` performs **0 datastore reads, 0 cache reads** (CountingStore,
    as in every service's hot-path test). `/readyz` DOES touch dependencies.
  * **Guard:** rendered manifests must map `livenessProbe → /healthz` and
    `readinessProbe → /readyz`. The reverse fails the test.
* **`helm template` purity.** Rendering must be offline and deterministic, or `helm diff`
  lies and GitOps flaps.
  * **Benchmark:** render twice → **byte-identical**. Render with no cluster reachable →
    succeeds. No `lookup`, no timestamps, no randomness in templates.

### (2) Every knob has a consumer — bidirectional

* Every key in `values.yaml` must **change the rendered output** when mutated. A value nothing
  reads is a lie in a config file. (Mutation guard.)
* Every `env:` name the chart injects must exist as a field on that service's `Settings`.
  **Kubernetes happily sets an env var nobody reads** — `MAX_GROUP` for `MAX_GROUPS` deploys
  clean, is silently ignored, and surfaces as a wrong number. Only possible because our
  services are self-describing: the test imports each `Settings` and diffs.
* Every **required** `Settings` field (no default) must be supplied by the chart or the render
  fails.

### (3) What production needs that the checklist omits — listed, then built

| Need | Why |
| --- | --- |
| `resources.requests` **required** when HPA is enabled | utilization is a % *of the request*; with no request the HPA silently never scales. kobo sets this correctly in its cloud overlay — but nothing *enforces* the pairing, so an overlay written without it renders green and never scales |
| CNI policy **preflight** that sends a packet | NetworkPolicy on stock K3s/flannel is inert and fails **open**. kobo installs Calico and checks a policy CNI is *present*; presence is not proof of *enforcement*, and its check can be waived interactively |
| Default-deny NetworkPolicy **for services**, per-service egress allow | kobo's cloud base fences pods; we make deny-by-default the chart default for every service, with egress opt-in |
| **No PDB when `replicas: 1`** | `minAvailable: 1` on a singleton blocks node drains forever |
| `preStop` sleep + `terminationGracePeriodSeconds` | endpoint removal is async → 502s on every deploy |
| Ingress **must not** expose `/internal/*` or `/metrics` | internal tier is token-guarded, not internet-facing |
| `automountServiceAccountToken: false` | none of our 16 services talk to the K8s API |
| **CronJobs for retention endpoints we already shipped** | `analytics/prune-raw`, `audit/enforce-retention`, compliance sweeps have no caller today |
| Digest-pinned images | `:latest` + `IfNotPresent` silently redeploys a stale image |
| memory limit == request; **no CPU limit** | OOM protection without latency throttling |
| `podAntiAffinity` + `topologySpreadConstraints` | two "HA" replicas on one node is not HA |
| Secrets via `secretKeyRef` only; `INTERNAL_SERVICE_TOKEN` length-validated | never a plaintext ConfigMap |
| `metrics-server` in bootstrap | HPA is inert without it |

### (4) The correctness footgun

> **`replicas > 1` with any `memory` driver silently breaks correctness.**

Every generation-keyed cache we built (analytics, authz PDP, feature-flag, search, compliance,
connector) invalidates by bumping a counter **in the cache**. With `CACHE_DRIVER=memory` that
cache is **per-pod**: pod A bumps its own counter, pod B serves stale results until TTL.
`MemoryRateLimiter` multiplies its limit by the replica count. `MemoryQueue` / `MemoryEventBus`
drop work that landed on another pod. None of this raises; it returns wrong numbers, under
load, in production only.

**The chart refuses to render `replicas > 1` unless `datastore`, `cache`, `eventbus`, `queue`
and `ratelimiter` are all shared drivers** (postgres/redis) — a deploy-time `ConfigError`,
exactly as an unknown driver is a boot-time `ConfigError`.

Runner-ups, each with a guard test: HPA utilization without requests · liveness on `/readyz` ·
PDB on a singleton · inert NetworkPolicy · `/internal` or `/metrics` exposed via Ingress ·
`readOnlyRootFilesystem` with no `/tmp` emptyDir · `:latest` tag.

---

## 2. Shape

```
deploy/
  chart/                      # ONE generic chart, used by all 16 services
    Chart.yaml
    values.yaml               # documented defaults (safe, single-replica, memory drivers)
    values.schema.json        # hard schema — unknown key = render error
    templates/
      _helpers.tpl            # names, labels, the guard `fail` assertions
      deployment.yaml         # probes, securityContext, preStop, anti-affinity
      service.yaml
      ingress.yaml            # /api/v1 + / only — never /internal, never /metrics
      configmap.yaml          # non-secret Settings knobs
      secret.yaml             # optional; prefer existingSecret
      networkpolicy.yaml      # default-deny + explicit allows
      hpa.yaml                # only if resources.requests.cpu is set
      pdb.yaml                # only if replicas > 1
      serviceaccount.yaml     # automount: false
      servicemonitor.yaml     # /metrics, cluster-internal only
      cronjob.yaml            # retention/prune callers (internal token)
  services/<name>.values.yaml # ~20 lines each x 16
  k3s/
    bootstrap.sh              # k3s + policy-capable CNI + metrics-server + cert-manager
    preflight.sh              # PROVES NetworkPolicy is enforced; fails loud
    platform/                 # postgres, redis, minio, traefik, sealed-secrets
  Makefile                    # make up / make deploy / make verify / make down
tests/deploy/                 # renders + guards (pytest, no cluster needed)
```

## 3. Steps

| # | Step |
| --- | --- |
| 24.1 | `deploy/chart` skeleton + `values.schema.json` (unknown key = error) |
| 24.2 | `_helpers.tpl` — the guard assertions (`fail`): shared-driver rule, HPA-needs-requests, PDB-needs-replicas, ingress-path denylist |
| 24.3 | deployment/service/serviceaccount — probes, securityContext, preStop, anti-affinity, digest pin |
| 24.4 | configmap/secret — Settings-driven env, `existingSecret`, token length check |
| 24.5 | networkpolicy — default-deny + per-service egress (internet only where required) |
| 24.6 | hpa / pdb / servicemonitor / ingress / cronjob (retention callers) |
| 24.7 | `deploy/services/*.values.yaml` × 16, generated from each `Settings` + manifest |
| 24.8 | `k3s/bootstrap.sh` + `preflight.sh` (CNI enforcement proof) + platform deps |
| 24.9 | `Makefile` (`up`/`deploy`/`verify`/`down`) + `scripts/create_service.py` emits values |
| 24.10 | Tests: render purity (byte-identical ×2, offline) · probe mapping · env⊆Settings · values mutation guard · every footgun above renders an error |
| 24.11 | `/healthz` zero-dependency benchmark in `_template` (propagated to all 16) |
| 24.12 | Verify: `make up` on real K3s, all 16 Ready, preflight green, docs + memory |

## 4. Out of scope

Cloud-managed K8s (EKS/GKE/AKS) overlays · service mesh · multi-cluster · a UI. The chart is
plain Kubernetes, so any of these layer on top without changes here.

## 5. Learned from kobo — kept, and enforced

**Kept:** `startupProbe` with a generous budget so a slow first boot isn't restart-looped by
liveness (with `timeoutSeconds: 5`, because probes touch a DB and the 1s default SIGTERMs
healthy pods) · singleton workers in their own Deployment, never one per web replica, and
deliberately excluded from the PDB · `runAsNonRoot` + `readOnlyRootFilesystem` +
`drop: [ALL]` + `seccompProfile: RuntimeDefault` + `emptyDir` at `/tmp` · secrets by
`secretKeyRef`, never inline · explicit `env` overriding `envFrom` so a re-apply cannot
silently fall back to a weaker backend.

**Two claims I made and had to retract** (recorded because the retraction is the lesson):

1. *"Their HPA is inert — it scales on CPU utilization with no CPU request."* **False.**
   `overlays/cloud/patch-cloud-api.yaml` sets `requests: {cpu: 100m, memory: 256Mi}` in the
   same overlay as the HPA, and its comment says exactly why. I had grepped `resources:` across
   the tree, piped it through `head -20`, and read the truncation as absence. *Never conclude
   "absent" from a truncated search.*
2. *"Their NetworkPolicy is inert — flannel doesn't enforce it."* **False for the cloud stack.**
   `install/linux-setup.sh` provisions k3s **with Calico**, and `install-cloud.sh` refuses to
   proceed without a policy CNI. The flannel caveat I quoted lives in `kobo/installer/k8s` —
   the **on-prem** channel, a different deployment path whose primary control is an air-gap.
   *Do not attribute one channel's caveat to another channel's stack.*

**What genuinely differs (enforcement, not correctness):** kobo gets both of these right *by
convention* — the right values happen to be in the right overlay. Nothing stops the next
overlay from omitting `requests.cpu` beside an HPA and rendering green. And `install-cloud.sh`
detects a policy CNI with `kubectl get pods -A | grep -qiE 'calico|cilium'` — that is
**presence**, not **enforcement** — and an operator can wave it through at the
`Continue anyway? [y/N]` prompt.

So this chart turns their conventions into **render-time errors** (`fail` in `_helpers.tpl`),
and `preflight.sh` proves policy enforcement by *sending a packet that must be refused* rather
than by looking for a pod name. Same destination; a guardrail instead of a habit.

**Generalised:** kobo hand-writes a manifest per service (`20-cloud-api`, `40-zatca`,
`25-advice-worker`). At 16 services that is 16 chances to forget a probe. Our services already
share one contract (`/healthz`, `/readyz`, `/metrics`, `/internal/manifest`, config-driven
ports, zero-hardcoded env), so **one chart + a short values file per service** is the natural
shape — and the manifest tells the chart what to wire.

---

**STATUS: ✅ ALL DONE — 140 chart guards + 80 probe-contract tests (5 × 16 services), all green. `helm lint` clean for all 16 values files; render is byte-identical across runs and works with no cluster.**

Built: `deploy/chart` (one generic chart), `deploy/services/*.values.yaml` × 16 (generated by `scripts/gen_deploy_values.py`, now called from `create_service.py`), `deploy/k3s/{bootstrap,preflight}.sh`, `deploy/k3s/platform/datastores.yaml`, `deploy/Makefile`, `tests/deploy/test_chart_guards.py`.

Every guard was verified to actually FIRE (a guard that never fires is decoration): 12 unsafe configurations are refused at render time, 4 legitimate ones render.

**The mutation guard caught me shipping the exact defect this repo exists to prevent.** `image.tag` was a documented, configurable values knob that nothing read — I had rationalised it in a comment as "decoration". Every values key is now mutation-tested inside the context that enables its feature (testing `ingress.host` with ingress off only proves the `if` works), and a companion test fails if a new key is added without a mutation test. `image.tag` is deleted: the image is always resolved by digest.

**Correction recorded** (see §5): two defects I initially attributed to kobo were my own errors — its cloud overlay *does* set `requests.cpu` beside the HPA, and its cloud installer *does* provision Calico. I concluded "absent" from a `head`-truncated grep, and attributed the on-prem channel's flannel caveat to the cloud stack. The chart's value is turning kobo's correct-by-convention into correct-by-construction, plus a preflight that proves policy enforcement by sending a packet rather than by finding a pod name.

**Not yet done:** `make up` / `make verify` have not been run against a real K3s cluster (none available on this Windows machine). Everything that can be proven without a cluster is proven; cluster verification is the one open item.
