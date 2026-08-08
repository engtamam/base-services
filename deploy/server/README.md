# deploy/server — the plug-and-play server toolkit

Smart, idempotent scripts that stand up and roll the platform on a single-node k3s box. Generalized
from the deploy pipelines that products (Kobo, Raje3, imtithal) built ON TOP of base-services, and
lifted here so any product gets them for free.

Everything is **env-driven** and **product-agnostic**. A product consuming this platform sets a few
variables (or drops in optional hooks) and reuses these unchanged.

## The scripts

| Script | What it does | Status |
| --- | --- | --- |
| `setup.sh` | THE ONE COMMAND: provision → bootstrap → preflight (fatal) → hooks → rollout → boot-heal | ✅ |
| `update.sh` | Health-gated rollout: render-first, per-service **auto-rollback**, `--only`, node-image prune | ✅ |
| `provision-server.sh` | Blank Ubuntu → hardened single-node k3s (packages, UFW, fail2ban, SSH, swap, sysctl, Docker, Helm, k3s w/ Calico flags) — idempotent + resumable | ✅ |
| `boot-heal.sh` | systemd boot unit + 5-min watchdog: clear NodeLost pods after an ungraceful reboot | ✅ |
| `pull-images.sh` | Auto-detects PUBLIC images (no-op) or, for private ones, credential + `imagePullSecret` + node-pull verify + prewarm | ✅ |
| `gen-secrets.py` | Mint the platform secrets once (shared token/salt/KEKs + per-role DSNs) from a schema **auto-derived** off each config.py; preserves existing values on re-run | ✅ |
| `reset-platform.sh` | Idempotent datastore reset + verify — wipes the PVC **and** its k3s host dir (the stale-data trap), asserts the shared DB exists, re-rolls, verifies | ✅ |
| `install-autoscaling.sh` (VPA) | Optional add-on — the chart already ships **HPA**; VPA needs a vendored manifest bundle, so it is a focused follow-up | ◻ optional |

`make setup` and `make roll` (from `deploy/`) wrap the first two.

## Plug-and-play: env + hooks

**Env** (all optional, with sensible defaults): `PRODUCT_NAME`, `NAMESPACE`, `CHART`, `VALUES_DIR`,
`HEALTH_TIMEOUT`, `SKIP_PROVISION`, `DRY_RUN`. So a product overlaying the chart runs, e.g.:

```bash
PRODUCT_NAME=raje3 CHART=platform/deploy/chart VALUES_DIR=deploy/services \
  sudo -E bash deploy/server/setup.sh
```

**Hooks** — `setup.sh` runs these ONLY if present, so a product adds just what it needs without
editing the orchestrator:

- `deploy/hooks/pre-deploy.sh` — before the rollout (secrets generation, DB roles/migrations).
- `deploy/hooks/post-deploy.sh` — after the rollout (seed data).
- `deploy/server/provision-server.sh`, `pull-images.sh`, `boot-heal.sh` — picked up automatically
  when you drop them in.

Optional phases that have no script simply print `skip` and the run continues.

## Shared secrets (minted ONCE, byte-identical everywhere)

Some secret keys must hold the **same value across services** or the platform misbehaves subtly.
`gen-secrets.py` mints each once and writes it into every secret that declares it — do not set them
per-service by hand:

- `INTERNAL_SERVICE_TOKEN` — the token every `/internal` tier checks; a mismatch is a 401 wall.
- `PII_SALT` — the pseudonym salt; two values pseudonymise the same person two different ways.
- `TOKEN_HS256_SECRET` — the session-token secret (all issuers/verifiers must agree).
- `BACKUP_KEK` — seals backup archives at rest. It must be the **same value backup-service holds**
  on **every export-capable target**, or the archives it collects cannot be opened. A target with
  it unset refuses its export with `503 "BACKUP_KEK not configured …"` (never a clear-text archive).
- `POSTGRES_PASSWORD` — the DSN password and the StatefulSet's own password are the one shared value.

## Safety properties (kept deliberately)

- **preflight is FATAL** — a cluster that cannot enforce NetworkPolicy is a tenant-isolation hole,
  not a slow start.
- **render-first** — a values file that cannot render is caught before the cluster is touched.
- **auto-rollback** — a service that does not become healthy is returned to its exact previous
  revision (releases are digest-pinned, so the rollback is exact).
- **idempotent + `DRY_RUN=1`** — re-running is safe; a dry run prints the plan and changes nothing.

## Postgres connection pools (a fleet on one database)

Every service opens an asyncpg pool. Two facts make the sizing matter:

- **`min_size` is a sticky floor.** A pool fills to `min_size` on a service's first query and holds
  it for the process lifetime. asyncpg's default is `min_size=10` — so left alone, 24 services pin
  **240** backend connections against a postgres whose stock `max_connections` is **100**, and the
  fleet deadlocks on an ordinary request once enough services have been touched (not at startup —
  later, in production, for a reason that does not look like a database problem).
- **The floor is the guaranteed demand.** `services × DATASTORE_POOL_MIN` connections are held
  whether or not anything is busy; `services × DATASTORE_POOL_MAX` is the peak.

The defaults are therefore **lazy and small**: `DATASTORE_POOL_MIN=1`, `DATASTORE_POOL_MAX=5`,
`DATASTORE_POOL_TIMEOUT_SECONDS=10`, `DATASTORE_POOL_MAX_INACTIVE_LIFETIME_SECONDS=300` (idle
connections above the floor are returned to postgres). A pod becomes Ready on **one** connection,
and an idle service costs one, not ten. Raise `DATASTORE_POOL_MAX` per service (env) for genuinely
hot services — deliberately, against the arithmetic below.

**The arithmetic is a preflight, not a surprise.** `setup.sh` runs `preflight-db-pool.sh`, which
compares `services × pool_min` (and the peak) against the database's `max_connections`:
- it is **FATAL** if the guaranteed floor does not fit (a certain deadlock);
- it **warns** if only the peak does not fit (likely under load).
Run it standalone any time: `PG_MAX_CONNECTIONS=100 bash deploy/server/preflight-db-pool.sh`.

**Readiness names a full database distinctly from a down one.** `/readyz` reports
`datastore: "pool exhausted (max_connections reached)"` when postgres is up but full, rather than
`down` — so nobody spends an hour looking at a healthy database.

**Rollouts cannot deadlock on connections.** A single-replica service **replaces** on rollout
(`maxSurge:0`) rather than surging a second pod that would double its pool; a multi-replica service
keeps capacity (`maxSurge:1`, `maxUnavailable:0`).

**For real scale, put a pooler in front.** One postgres with stock settings is the shipped topology;
for many replicas of hot services, **pgbouncer in transaction mode** is the standard answer — it
lets generous per-service pools share a small backend connection count. The alternatives are a
higher `max_connections`, a database per heavy service, or a managed postgres with a larger ceiling.
Whichever you choose, the preflight will tell you before the deploy whether it fits.
