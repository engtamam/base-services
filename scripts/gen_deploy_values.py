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
    # The erasure fan-out (Phase 37). A DSAR dispatches immediately on process(); the sweep is the
    # SAFETY NET that re-drives whatever did not ack — a target that was down, restarting, or
    # answered 202 and is still working. Without it one unlucky moment during a fan-out leaves a
    # subject's data in place while the request sits open until its SLA quietly expires.
    # Retention runs nightly: it is measured in DAYS, so more often buys nothing.
    "compliance-service": [
        ("dsar-sweep", "*/5 * * * *", "/internal/compliance/dsar/run"),
        ("retention-purge", "17 3 * * *", "/internal/compliance/retention/run"),
    ],
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


# Non-identity env that belongs to the DEPLOYMENT rather than to config/product.json. It lives
# here, not hand-edited into the values file, because safety:wiring regenerates and diffs — a hand
# edit would be silently reverted on the next release and the fan-out would quietly stop.
# Every service records its privileged actions to audit-service, through the same config path a
# customer would use for their own log. The chain was empty for months because nothing was pointed
# at it; safety:audit-wiring now fails if a service declares actions and is not configured here.
AUDIT_ENV = {
    "AUDIT_SINK_DRIVER": "http",
    "AUDIT_SERVICE_URL": "http://audit-service:8000",
}

STATIC_ENV: dict[str, dict[str, str]] = {
    "compliance-service": {
        # The data map. OUR OWN services are registered through EXACTLY the config path a customer
        # uses for theirs — no internal fast path, no special case in the dispatcher. That makes
        # this list a worked example anyone can copy, and it means the path our erasures travel is
        # the path we ask customers to trust. safety:privacy-wiring fails if a service implements
        # the erase contract and is missing here.
        "PRIVACY_TRANSPORT_DRIVER": "http",
        "PRIVACY_TARGETS": ",".join(
            f"{svc}=http://{svc}:8000" for svc in (
                "identity-service", "billing-service", "file-service", "notification-service",
                "analytics-service", "audit-service", "authz-service", "compliance-service",
                "feature-flag-service", "search-service", "zatca-service", "wallet-service",
                "stream-service", "webhook-service", "job-service", "connector-service",
                "tenant-service", "api-gateway")),
        # Retention for the records compliance-service OWNS. Every other service declares its own
        # period in its RoPA row and enforces it itself — only the owner knows which timestamp is
        # its retention clock. Consent PROOF outlives everything else: it is the evidence that
        # answers "prove she agreed before you processed", and destroying it destroys the defence
        # for processing that already happened.
        "RETENTION_RULES": "dsar=1095,breaches=1825,consent_proof=2555",
    },
}


def _env_block(name: str, cfg: dict) -> str:
    """Product identity, as env vars. Generated — never hand-edited.

    The chart renders these into a ConfigMap, and tests/deploy/test_chart_guards.py refuses any
    key that is not a real field on this service's Settings. An empty product value is omitted
    rather than emitted as "", because an empty ConfigMap entry overrides the service's own
    default with nothing.
    """
    env = dict(product.env_for(name, cfg) or {})
    # audit-service points at ITSELF, deliberately. Its own auditable actions are purge, retention
    # and erasure — rare, and every one of them REMOVES from the log, so they are exactly the
    # actions that must be recorded before they happen. A loopback call costs nothing at that
    # volume, keeps one code path for every service, and means a purge the service cannot record
    # is a purge that does not happen.
    if name != "_template":
        env.update(AUDIT_ENV)
    env.update(STATIC_ENV.get(name, {}))
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
