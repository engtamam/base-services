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

# Services that read/write blobs and so may be configured with OBJECTSTORE_DRIVER=s3 against an
# IN-CLUSTER object store (MinIO / on-prem S3). Egress is deny-by-default, so without this a
# service configured for s3 is blackholed — the pod comes up, /readyz hangs, nothing names the
# cause. Opening egress to the object-store role on the S3 port here is harmless when the driver is
# local/memory (the rule permits traffic that never happens) and correct the moment it is s3. A
# CLOUD/out-of-cluster S3 endpoint needs `internet: true` instead — a different seam.
OBJECTSTORE_EGRESS = {
    "file-service",         # user uploads (small inline + large multipart)
    "backup-service",       # backup archives
    "stream-service",       # recordings / playback segments
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
    "backup-service": [
        ("run", "42 2 * * *", "/internal/backup/run"),
        # Watch for a missed/stale/unverified backup and alert — more often than the daily capture,
        # so a schedule that stops firing is caught within the hour, not the next day.
        ("health-check", "23 * * * *", "/internal/backup/health/check"),
        # Prune to the retention limits — never a target's last verified backup. Daily, offset.
        ("prune", "51 3 * * *", "/internal/backup/prune"),
        # The restore drill — prove, against the at-rest bytes, that every service still restores.
        # Weekly (Sunday 04:07): DR that is never rehearsed is DR that fails when it is needed.
        ("drill", "7 4 * * 0", "/internal/backup/drill"),
    ],
    "billing-service": [("dunning", "31 4 * * *", "/internal/billing/dunning"),
                        # The renewal engine — invoice subscriptions whose period has ended. Daily,
                        # offset from dunning so the two sweeps do not contend. Without it, year-two
                        # revenue is never invoiced.
                        ("cycle-due", "17 3 * * *", "/internal/billing/cycle-due"),
                        # Deliver due credit grants. Its handler is explicitly "driven by the
                        # platform's scheduler"; without this the grant never lands.
                        ("grants-drain", "*/5 * * * *", "/internal/billing/grants/drain")],
    "job-service": [
        ("scheduler-tick", "* * * * *", "/internal/scheduler/tick"),
        ("reclaim", "*/5 * * * *", "/internal/jobs/reclaim"),
    ],
    "webhook-service": [("sweep", "*/5 * * * *", "/internal/webhooks/sweep")],
    # Re-attempt queued deliveries. With NOTIFY_DELIVERY_MODE=queued a provider hiccup queues a
    # message, and without this tick `queued` is just a prettier word for `lost` — the delivery log
    # fills with queued while the service looks healthy. Drain in inline mode is a cheap no-op, so
    # this is safe to ship on always.
    "notification-service": [("drain", "*/1 * * * *", "/internal/notifications/drain")],
    # Without the sweep, a session past its guards (max_duration/idle/lease) or a zombie engine would
    # run forever; without retention, recordings would grow without bound.
    "stream-service": [
        ("sweep", "*/1 * * * *", "/internal/stream/sweep"),
        ("retention", "17 3 * * *", "/internal/stream/retention"),
    ],
    "zatca-service": [("sweep", "*/10 * * * *", "/internal/zatca/sweep")],
    # A verification prompt nobody answers must LAPSE rather than sit pending forever. status()
    # already refuses to trust a lapsed record, so this is tidiness and visibility, not correctness
    # — which is why every 15 minutes is ample.
    "verification-service": [
        ("sweep-pending", "*/15 * * * *", "/internal/verification/sweep-pending"),
    ],
    # The erasure fan-out (Phase 37). A DSAR dispatches immediately on process(); the sweep is the
    # SAFETY NET that re-drives whatever did not ack — a target that was down, restarting, or
    # answered 202 and is still working. Without it one unlucky moment during a fan-out leaves a
    # subject's data in place while the request sits open until its SLA quietly expires.
    # Retention runs nightly: it is measured in DAYS, so more often buys nothing.
    "compliance-service": [
        ("dsar-sweep", "*/5 * * * *", "/internal/compliance/dsar/run"),
        ("retention-purge", "17 3 * * *", "/internal/compliance/retention/run"),
        # Lapse consents past their validity so a withdrawn/expired basis stops permitting
        # processing. Its handler expects a nightly cron; without it a lapsed consent still reads
        # as granted.
        ("consent-sweep-lapses", "43 3 * * *", "/internal/compliance/consent/sweep-lapses"),
    ],
    # A conversation retention policy that is stored and never executed is the most common
    # compliance defect there is: every review passes because the setting exists, and the data is
    # still there years later. Nightly, because the policy is measured in days.
    "assistant-service": [("retention", "43 3 * * *", "/internal/assistant/retention")],
    # Without these the wallet's holds never expire: a client that dies between reserve and
    # confirm strands a customer's credits forever, while the balance still displays them.
    "wallet-service": [
        ("sweep-holds", "*/2 * * * *", "/internal/wallet/sweep-holds"),
        ("sweep-grants", "41 3 * * *", "/internal/wallet/sweep-grants"),
    ],
    # Erase tenants whose owner-close grace window has elapsed. Its handler is "driven by the
    # platform scheduler"; without this a closed tenant is suspended forever but NEVER erased — the
    # close promise (and its retention obligation) silently never completes. Daily: grace is in days.
    "tenant-service": [("close-sweep", "37 3 * * *", "/internal/tenants/close-sweep")],
    # Deliver queued ticket notifications. Scheduler-driven so a restart cannot lose the retry;
    # without it a queued ticket notification is never sent.
    "ticket-service": [("notifications-drain", "*/2 * * * *", "/internal/tickets/notifications/drain")],
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
    datastore: true{objectstore_block}
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
# audit-service audits ITSELF, in-process — never over HTTP to itself (see _env_block). No URL:
# in_process needs no network target, and emitting an empty AUDIT_SERVICE_URL would override the
# service default with nothing.
AUDIT_SELF_ENV = {
    "AUDIT_SINK_DRIVER": "in_process",
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
                "tenant-service", "api-gateway", "verification-service",
                "onboarding-service", "assistant-service", "backup-service")),
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
    # Every service records its privileged actions to audit-service over HTTP — except
    # audit-service itself, which records IN-PROCESS.
    #
    # It used to point its http sink at itself ("a loopback costs nothing, one code path for
    # everyone"). That reasoning was wrong and it was a real hang: an erase handler occupies a
    # worker while it POSTs subject.erased back to the same service, so under load — several DSARs,
    # or the verify cron firing during erases — every worker is busy and the self-call finds no
    # free loop, times out, and the critical action fail-closes with a 503. Adding workers only
    # widens the window. audit-service records to its own core directly, which is faster, cannot
    # deadlock, and is more correct: a hash-chain append should never depend on a network round
    # trip that can fail half-way and cannot be safely retried.
    if name == "audit-service":
        env.update(AUDIT_SELF_ENV)
    elif name != "_template":
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
        # Be precise about which layer narrows by HOST: the app's EGRESS_ALLOWED_HOSTS does; this
        # NetworkPolicy does not. It opens 80/443 to every public IP (RFC1918/link-local/metadata
        # still excluded). Do not read `internet: true` as a per-host allow-list.
        note = ("        # third-party caller: host allow-listing is APPLICATION-layer "
                "(EGRESS_ALLOWED_HOSTS); this policy opens 80/443 to any public IP")

    # In-cluster object-store egress for the blob-users. Harmless when the driver is local/memory;
    # required the moment OBJECTSTORE_DRIVER=s3 points at an in-cluster endpoint.
    objectstore_block = ""
    if name in OBJECTSTORE_EGRESS:
        objectstore_block = (
            "\n    objectStore:"
            "\n      enabled: true          # reads/writes blobs; opens egress to the S3 API port"
            "\n      port: 9000"
            "\n      roleLabel: objectstore")
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
        objectstore_block=objectstore_block,
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
