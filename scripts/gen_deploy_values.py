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

import product        # config/product.json -> per-service env. See scripts/product.py.

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
    # Large-object path (Phase 34): drain the scan queue so staged uploads become readable, and abort
    # abandoned upload sessions so their multiparts stop costing bucket storage.
    "file-service": [
        ("scan", "*/1 * * * *", "/internal/files/scan"),
        ("sweep-uploads", "*/15 * * * *", "/internal/files/sweep-uploads"),
    ],
    "billing-service": [("dunning", "31 4 * * *", "/internal/billing/dunning")],
    "job-service": [
        ("scheduler-tick", "* * * * *", "/internal/scheduler/tick"),
        ("reclaim", "*/5 * * * *", "/internal/jobs/reclaim"),
    ],
    "webhook-service": [("sweep", "*/5 * * * *", "/internal/webhooks/sweep")],
    # Without the sweep, a session past its guards (max_duration/idle/lease) or a zombie engine would
    # run forever; without retention, recordings would grow without bound.
    "stream-service": [
        ("sweep", "*/1 * * * *", "/internal/stream/sweep"),
        ("retention", "17 3 * * *", "/internal/stream/retention"),
    ],
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
{env_block}{ingress_block}{cron_block}"""

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


def _env_block(name: str, cfg: dict) -> str:
    """Product identity, as env vars. Generated — never hand-edited.

    The chart renders these into a ConfigMap, and tests/deploy/test_chart_guards.py refuses any
    key that is not a real field on this service's Settings. An empty product value is omitted
    rather than emitted as "", because an empty ConfigMap entry overrides the service's own
    default with nothing.
    """
    env = product.env_for(name, cfg)
    if not env:
        return ""
    lines = ["", "# From config/product.json — regenerate with scripts/gen_deploy_values.py.",
             "# Do not hand-edit: `identifiers` are frozen and guarded by safety:product.", "env:"]
    lines += [f"  {k}: {v!r}".replace("'", '"') for k, v in sorted(env.items())]
    return "\n".join(lines) + "\n"


def render(name: str, cfg: dict) -> str:
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
        env_block=_env_block(name, cfg),
        ingress_ns="ingress" if name in PUBLIC_INGRESS else '""',
        internet=str(internet).lower(), internet_note=note,
        ingress_block=ingress_block, cron_block=cron_block,
    )


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfg, source = product.load()
    names = sorted(p.name for p in SERVICES_DIR.iterdir()
                   if p.is_dir() and p.name != "_template" and (p / "app").is_dir())
    for name in names:
        # newline="\n": .gitattributes mandates LF, and CRLF in a generated file makes the
        # determinism gate see a diff that is not there.
        (OUT_DIR / f"{name}.values.yaml").write_text(
            render(name, cfg), encoding="utf-8", newline="\n")
    wired = sum(1 for n in names if product.env_for(n, cfg))
    print(f"wrote {len(names)} values files to {OUT_DIR}")
    print(f"product identity from {source.name}: {wired} service(s) inherited env values")
    return 0


if __name__ == "__main__":
    sys.exit(main())
