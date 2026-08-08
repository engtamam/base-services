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
| `pull-images.sh` | Private-registry credential + `imagePullSecret` + prewarm (base images are public) | ◻ planned |
| `gen-secrets.py` | Mint shared secrets once + per-role DSNs, from a schema derived off the chart | ◻ planned |

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

## Safety properties (kept deliberately)

- **preflight is FATAL** — a cluster that cannot enforce NetworkPolicy is a tenant-isolation hole,
  not a slow start.
- **render-first** — a values file that cannot render is caught before the cluster is touched.
- **auto-rollback** — a service that does not become healthy is returned to its exact previous
  revision (releases are digest-pinned, so the rollback is exact).
- **idempotent + `DRY_RUN=1`** — re-running is safe; a dry run prints the plan and changes nothing.
