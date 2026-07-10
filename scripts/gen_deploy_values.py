#!/usr/bin/env python
"""Emit deploy/services/<name>.values.yaml for every service.

Kept as a script (not a one-off) so `create_service.py` can call it: a new service is
deployable the moment it exists, instead of the moment someone remembers to write YAML.

Facts encoded here — and nowhere else:
  * which services must reach the public internet (they call third parties),
  * which expose a public ingress (only the gateway),
  * which internal endpoints need a scheduled caller (retention/sweeps that otherwise
    never run — an unenforced retention policy is a compliance finding, not a TODO).
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SERVICES_DIR = ROOT / "services"
OUT_DIR = ROOT / "deploy" / "services"

REGISTRY = "ghcr.io/base-services"
PLACEHOLDER_DIGEST = "sha256:" + "0" * 64   # replaced by CI at publish time

# Services that must call third parties. Their in-app SSRF guards still apply; the
# NetworkPolicy is the second layer. Everything else gets NO internet egress at all.
INTERNET_EGRESS = {
    "webhook-service",      # customer-supplied URLs (SSRF-guarded, IP-pinned)
    "connector-service",    # Slack/HubSpot/OAuth2 providers
    "notification-service", # Unifonic/Taqnyat/SMTP/push
    "billing-service",      # Stripe/Moyasar/Tap/PayTabs/...
    "zatca-service",        # ZATCA Fatoora endpoints
}

# Only the gateway is public. Every other service is reachable service-to-service.
PUBLIC_INGRESS = {"api-gateway"}

# Endpoints that exist today with no caller. Schedules are staggered so a cluster of
# 16 services does not stampede its database on the hour.
CRONS: dict[str, list[tuple[str, str, str]]] = {
    "analytics-service": [("prune-raw", "17 3 * * *", "/internal/analytics/prune-raw")],
    "audit-service": [("enforce-retention", "23 3 * * *", "/internal/audit/enforce-retention")],
    "billing-service": [("dunning", "31 4 * * *", "/internal/billing/dunning")],
    "job-service": [
        ("scheduler-tick", "* * * * *", "/internal/scheduler/tick"),
        ("reclaim", "*/5 * * * *", "/internal/jobs/reclaim"),
    ],
    "webhook-service": [("sweep", "*/5 * * * *", "/internal/webhooks/sweep")],
    "zatca-service": [("sweep", "*/10 * * * *", "/internal/zatca/sweep")],
    # Without these the wallet's holds never expire: a client that dies between reserve and
    # confirm strands a customer's credits forever, while the balance still displays them.
    "wallet-service": [
        ("sweep-holds", "*/2 * * * *", "/internal/wallet/sweep-holds"),
        ("sweep-grants", "41 3 * * *", "/internal/wallet/sweep-grants"),
    ],
}

TEMPLATE = """# {name} — deployment values.
#
# Defaults are SAFE, not scalable: one replica, in-memory drivers. To scale out, move every
# stateful port to a shared driver below; the chart REFUSES to render replicas>1 otherwise,
# because an in-memory cache is per-pod and our generation-keyed invalidation would silently
# serve stale reads.
service:
  name: {name}
  port: 8000

image:
  repository: {registry}/{name}
  digest: "{digest}"   # CI rewrites this at publish; never a :latest tag

replicas: 1

drivers:
  datastore: postgres
  cache: redis
  eventbus: redis
  queue: redis
  ratelimiter: redis

resources:
  requests:
    cpu: 50m
    memory: 128Mi
  limits:
    memory: 256Mi

autoscaling:
  enabled: false        # flip on once you have load; requests.cpu above makes it real

existingSecret: {name}-secrets   # INTERNAL_SERVICE_TOKEN, DATASTORE_DSN, CACHE_URL, TOKEN_*

networkPolicy:
  enabled: true
  ingressFromNamespace: {ingress_ns}
  egress:
    dns: true
    datastore: true
    internet: {internet}{internet_note}

metrics:
  serviceMonitor: true
{ingress_block}{cron_block}"""

INGRESS_BLOCK = """
ingress:
  enabled: true
  className: traefik
  host: api.example.com     # CHANGE ME
  paths:
    - /api/v1               # the chart refuses /internal, /metrics and a bare "/"
  tls:
    enabled: true
"""

NO_INGRESS_BLOCK = """
# No ingress: reached service-to-service on the internal tier only.
ingress:
  enabled: false
"""


def render(name: str) -> str:
    internet = name in INTERNET_EGRESS
    note = ""
    if internet:
        note = "        # calls third parties; in-app SSRF guard is the first layer"
    ingress_block = INGRESS_BLOCK if name in PUBLIC_INGRESS else NO_INGRESS_BLOCK

    cron_block = ""
    if name in CRONS:
        lines = ["\n# These internal endpoints have NO caller without this block.", "cronJobs:"]
        for job, schedule, path in CRONS[name]:
            lines.append(f"  - name: {job}")
            lines.append(f"    schedule: {schedule!r}".replace("'", '"'))
            lines.append(f"    path: {path}")
        cron_block = "\n".join(lines) + "\n"

    return TEMPLATE.format(
        name=name, registry=REGISTRY, digest=PLACEHOLDER_DIGEST,
        ingress_ns="ingress" if name in PUBLIC_INGRESS else '""',
        internet=str(internet).lower(), internet_note=note,
        ingress_block=ingress_block, cron_block=cron_block,
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    names = sorted(p.name for p in SERVICES_DIR.iterdir()
                   if p.is_dir() and p.name != "_template" and (p / "app").is_dir())
    for name in names:
        (OUT_DIR / f"{name}.values.yaml").write_text(render(name), encoding="utf-8")
    print(f"wrote {len(names)} values files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
