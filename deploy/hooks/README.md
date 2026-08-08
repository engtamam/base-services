# deploy/hooks — the plug-and-play extension seam

`setup.sh` runs these **only if present**, so a product adds just what it needs without editing the
orchestrator.

| Hook | When | Ships as |
| --- | --- | --- |
| `pre-deploy.sh` | before the rollout | mints platform secrets via `gen-secrets.py` (extend for DB roles/migrations) |
| `post-deploy.sh` | after the rollout | no-op (base-services self-provisions; extend for seeds) |

A product overlaying the platform edits these two files (and drops any of `provision-server.sh`,
`pull-images.sh`, `boot-heal.sh`, a `provision-db.sh`, a `seed/` into `deploy/`) — everything else in
`deploy/server/` is reused unchanged and driven by `PRODUCT_NAME` + env.
