# deploy — plug-and-play K3s

One generic Helm chart for all 16 services. Each service brings a ~30-line values file.
Adding a service means writing that file — not another Deployment, Service, Ingress,
NetworkPolicy, HPA, PDB and CronJob by hand.

```bash
cd deploy
make up        # bootstrap K3s: Calico + metrics-server + postgres/redis
make verify    # PROVE the guarantees hold on this cluster (see below)
make deploy    # install/upgrade all 16 services
make status
```

`make deploy` depends on `make verify`. We do not install a platform whose deny-by-default
is decorative or whose autoscalers can never scale.

## Egress and the object store

Egress is **deny-by-default**; each allow is opt-in per service under `networkPolicy.egress`:
`dns`, `datastore` (postgres/redis by label), `internet` (SSRF-guarded third-party callers),
`objectStore` (an in-cluster S3 on its API port), and `extra` — a verbatim list for anything else a
service must reach. A dependency no allow opens no longer hangs silently: `/readyz` bounds each probe
and answers `{"<dep>": "timeout"}`.

`internet: true` is **all-or-nothing at the network layer** — it opens 80/443 to every public IP
(RFC1918 / link-local / metadata excluded), because a stock Kubernetes NetworkPolicy can match egress
only by IP/CIDR, never by hostname. Narrowing by **host** is done in the application: the SSRF guard
plus `EGRESS_ALLOWED_HOSTS`. Do not read `internet: true` as a per-host allow-list.

For real **network-layer per-hostname** egress, set `networkPolicy.egress.calicoFqdn.enabled: true` and
list `calicoFqdn.hosts: [...]`. This renders a Calico `NetworkPolicy` (`projectcalico.org/v3`) that
allows egress **only** to those domains (plus DNS) — so you drop `internet: true` and let it be the
allow-list. It is **opt-in and CNI-specific**: it requires Calico with DNS policy enabled (our
bootstrap installs Calico, but DNS policy needs Felix's `DNSPolicyMode` — validate enforcement in
your cluster with `preflight.sh`-style checks before relying on it). Any other CNI ignores it.

The blob services (`file`, `backup`, `stream`) default to a **local** object driver. For S3, set
`OBJECTSTORE_DRIVER=s3` + `OBJECTSTORE_ENDPOINT_URL` / `_ACCESS_KEY` / `_SECRET_KEY` / `_BUCKET`
(+ `OBJECTSTORE_FORCE_PATH_STYLE=true` for a path-style endpoint) — your own MinIO / on-prem / cloud
S3. Or run the bundled reference with `OBJECTSTORE_ENABLED=1 deploy/k3s/bootstrap.sh`: **SeaweedFS**
(Apache-2.0; *not* MinIO — MinIO's server is AGPLv3, outside our permissive-only licence policy). It
carries the `base-services/role: objectstore` label the `objectStore` egress selects, and needs an
`objectstore-secrets` Secret — see `deploy/k3s/objectstore.yaml` and `objectstore.s3.example.json`.

## The chart refuses to render mistakes

Kubernetes accepts every mistake below and gives you a green rollout. You find out from wrong
numbers, a restart storm, or a node drain that hangs. So the chart fails at render time:

| Refused | Why it is not merely untidy |
| --- | --- |
| `replicas > 1` with any `memory` driver | our generation-keyed caches invalidate by bumping a counter **in the cache**; an in-memory cache is per-pod, so pod A's write never invalidates pod B. Stale reads, multiplied rate limits, dropped queue work — silently, only under load |
| HPA enabled without `resources.requests.cpu` | utilization is a % *of the request*; with no request the HPA reports `<unknown>/70%` and never scales |
| `resources.limits.cpu` | a CPU limit throttles the service under exactly the load you wanted it to survive |
| `podDisruptionBudget.minAvailable` ≥ replicas | the budget can never be satisfied, so every node drain hangs forever |
| Ingress path outside `/api/v1` (incl. `/`, `/internal`, `/metrics`) | the internal tier is guarded by a shared token, not by being on the internet |
| Ingress enabled with `networkPolicy.enabled=false` | an internet-reachable pod with unrestricted egress turns one SSRF into exfiltration |
| `cronJobs` without an `existingSecret` | the internal token would have to live in a ConfigMap |
| an image without a `sha256:` digest | `:latest` + `IfNotPresent` silently redeploys the image already on the node |
| any unknown values key | a typo is a typo, not a feature (`values.schema.json`, `additionalProperties: false`) |

All of it is pinned by `tests/deploy/test_chart_guards.py` (92 tests, no cluster required).

## Bootstrap installs. Preflight proves.

`preflight.sh` does not check that Calico is *present* — presence proves it was installed, not
that this policy blocks this packet. It sends the packet:

1. **baseline** — client can reach server (networking works)
2. **deny** — apply default-deny, client *cannot* (policy is enforced)
3. **restore** — remove the policy, client can again (we broke nothing)

Only that order proves enforcement. Skipping step 1 means a *broken cluster* reports "enforced,"
because the connection fails either way. It also asserts the metrics API actually serves data,
since an HPA without it is inert with a green rollout.

## What each service gets, without asking

- `livenessProbe → /healthz`, `readinessProbe → /readyz`, plus a generous `startupProbe`
  budget so a slow first boot isn't restart-looped. `/healthz` touches **no dependency** —
  proven in every service by `tests/integration/test_probe_contract.py`. Point liveness at
  `/readyz` and one slow Postgres moment restarts the whole fleet.
- `runAsNonRoot`, `readOnlyRootFilesystem` (+ an `emptyDir` at `/tmp`, or it crash-loops),
  `drop: [ALL]`, `seccompProfile: RuntimeDefault`, `automountServiceAccountToken: false`.
- `maxUnavailable: 0` and a `preStop` pause — endpoint removal is asynchronous, so without it
  every deploy serves 502s.
- Default-deny NetworkPolicy. Egress to the public internet is opt-in and granted only to the
  five services that call third parties; even then RFC1918 and `169.254.0.0/16` (cloud
  metadata) stay excluded, so a bypassed in-app SSRF guard still cannot pivot.
- A ConfigMap checksum annotation, so a config change actually rolls the pods.

## CronJobs

We shipped retention and sweep endpoints and nothing ever called them. An unenforced retention
policy is a compliance finding, not a TODO. The chart schedules them:

| Service | Endpoint | Schedule |
| --- | --- | --- |
| analytics | `/internal/analytics/prune-raw` | `17 3 * * *` |
| audit | `/internal/audit/enforce-retention` | `23 3 * * *` |
| billing | `/internal/billing/dunning` | `31 4 * * *` |
| job | `/internal/scheduler/tick`, `/internal/jobs/reclaim` | `* * * * *`, `*/5 * * * *` |
| webhook | `/internal/webhooks/sweep` | `*/5 * * * *` |
| zatca | `/internal/zatca/sweep` | `*/10 * * * *` |

Staggered on purpose: sixteen services must not stampede one database on the hour. Each job
runs the service's **own digest-pinned image** and the Python stdlib, so there is no second
image to pin, scan, and keep CVE-free just to make one HTTP request.

## Scaling out

Defaults are safe, not scalable: one replica. To scale, move every stateful port to a shared
driver *first* — the chart enforces this, it is not advice:

```yaml
replicas: 3
drivers: { datastore: postgres, cache: redis, eventbus: redis, queue: redis, ratelimiter: redis }
autoscaling: { enabled: true, minReplicas: 2, maxReplicas: 6 }
```

## Adding a service

`scripts/create_service.py` emits `deploy/services/<name>.values.yaml` automatically, so the
deploy guards cover a new service from its first commit. Regenerate all of them with
`python scripts/gen_deploy_values.py`.

## Requirements

`kubectl`, `helm` ≥ 3.8. CI must provide `helm`, or `tests/deploy` skips (loudly) and the
guards do not run.
