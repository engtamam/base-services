"""Chart guards — run `helm template` and assert what it does, and what it REFUSES to do.

No cluster required. These are the deploy-time equivalent of each service's config-knob
guard: a chart value that nothing reads, or a manifest that renders green while being
silently wrong, is exactly the class of defect this repo exists to prevent.

Install helm, or point HELM_BIN at the binary.
"""
from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHART = ROOT / "deploy" / "chart"
VALUES_DIR = ROOT / "deploy" / "services"
SERVICES_DIR = ROOT / "services"

DIGEST = "sha256:" + "a" * 64
BASE = [
    "--set", "service.name=analytics-service",
    "--set", "image.repository=ghcr.io/bs/analytics",
    "--set", f"image.digest={DIGEST}",
]

HELM = shutil.which("helm") or __import__("os").environ.get("HELM_BIN")
pytestmark = pytest.mark.skipif(
    not HELM, reason="helm not installed — chart guards cannot run (CI must provide it)"
)


def render(*extra: str, values_file: pathlib.Path | None = None) -> str:
    cmd = [HELM, "template", "t", str(CHART)]
    if values_file:
        cmd += ["-f", str(values_file)]
    else:
        cmd += BASE
    cmd += list(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(f"render failed:\n{proc.stderr}")
    return proc.stdout


def render_fails(*extra: str) -> str:
    cmd = [HELM, "template", "t", str(CHART)] + BASE + list(extra)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode != 0, f"expected a render ERROR, but it rendered:\n{proc.stdout[:400]}"
    return proc.stderr


def docs(rendered: str) -> list[dict]:
    return [d for d in yaml.safe_load_all(rendered) if d]


def kinds(rendered: str) -> set[str]:
    return {d["kind"] for d in docs(rendered)}


def find(rendered: str, kind: str) -> dict | None:
    for d in docs(rendered):
        if d["kind"] == kind:
            return d
    return None


SHARED = [
    "--set", "drivers.datastore=postgres", "--set", "drivers.cache=redis",
    "--set", "drivers.eventbus=redis", "--set", "drivers.queue=redis",
    "--set", "drivers.ratelimiter=redis",
]


# ── purity: helm diff / GitOps depend on it ─────────────────────────────────
def test_render_is_deterministic():
    assert render() == render(), "chart render is not byte-identical across runs"


def test_render_needs_no_cluster():
    import os

    env = dict(os.environ, KUBECONFIG="/nonexistent-kubeconfig")
    proc = subprocess.run([HELM, "template", "t", str(CHART)] + BASE,
                          capture_output=True, text=True, env=env)
    assert proc.returncode == 0, "chart cannot render offline (a `lookup` crept in?)"


def test_chart_uses_no_lookup_function():
    """Helm's `lookup` makes a render depend on live cluster state — never reproducible.

    Matches the template ACTION (`{{ ... lookup ... }}`), not the English word: the network
    policy legitimately explains that a DNS *lookup* hangs without egress to kube-dns.
    """
    action = re.compile(r"\{\{[^}]*\blookup\b[^}]*\}\}")
    for tpl in (CHART / "templates").rglob("*"):
        if tpl.suffix not in (".yaml", ".tpl"):
            continue
        text = tpl.read_text(encoding="utf-8")
        text = re.sub(r"\{\{/\*.*?\*/\}\}", "", text, flags=re.DOTALL)   # drop {{/* comments */}}
        assert not action.search(text), f"{tpl.name} calls the helm `lookup` function"


# ── THE probe contract (see services' tests/integration/test_probe_contract.py) ──
def test_liveness_is_healthz_and_readiness_is_readyz():
    dep = find(render(), "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["livenessProbe"]["httpGet"]["path"] == "/healthz"
    assert c["startupProbe"]["httpGet"]["path"] == "/healthz"
    assert c["readinessProbe"]["httpGet"]["path"] == "/readyz"


def test_liveness_never_points_at_readyz():
    """A liveness probe on /readyz turns one slow DB moment into a fleet-wide restart."""
    dep = find(render(), "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    for probe in ("livenessProbe", "startupProbe"):
        assert c[probe]["httpGet"]["path"] != "/readyz"


def test_probe_timeout_is_not_the_1s_default():
    dep = find(render(), "Deployment")
    c = dep["spec"]["template"]["spec"]["containers"][0]
    assert c["readinessProbe"]["timeoutSeconds"] >= 2


# ── the correctness footgun: scale-out with per-pod state ───────────────────
@pytest.mark.parametrize("driver", ["datastore", "cache", "eventbus", "queue", "ratelimiter"])
def test_scaling_out_with_any_memory_driver_is_refused(driver):
    args = list(SHARED)
    idx = args.index(f"drivers.{driver}=postgres") if f"drivers.{driver}=postgres" in args \
        else args.index(f"drivers.{driver}=redis")
    args[idx] = f"drivers.{driver}=memory"
    err = render_fails("--set", "replicas=3", *args)
    assert "UNSAFE SCALE-OUT" in err and driver in err


def test_scaling_out_with_all_shared_drivers_is_allowed():
    assert "Deployment" in kinds(render("--set", "replicas=3", *SHARED))


def test_autoscaling_also_triggers_the_shared_driver_guard():
    """minReplicas=2 is a scale-out even though `replicas` is still 1."""
    err = render_fails("--set", "autoscaling.enabled=true")
    assert "UNSAFE SCALE-OUT" in err


# ── HPA needs a CPU request or it silently never scales ─────────────────────
def test_hpa_without_cpu_request_is_refused():
    err = render_fails("--set", "autoscaling.enabled=true", *SHARED,
                       "--set", "resources.requests.cpu=null")
    assert "HPA IS INERT" in err


def test_hpa_with_cpu_request_renders_and_targets_utilization():
    out = render("--set", "autoscaling.enabled=true", *SHARED)
    hpa = find(out, "HorizontalPodAutoscaler")
    assert hpa["spec"]["metrics"][0]["resource"]["target"]["type"] == "Utilization"
    dep = find(out, "Deployment")
    assert dep["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"]["cpu"]


def test_deployment_omits_replicas_when_hpa_owns_the_count():
    """Setting replicas beside an HPA makes every `helm upgrade` fight the autoscaler."""
    dep = find(render("--set", "autoscaling.enabled=true", *SHARED), "Deployment")
    assert "replicas" not in dep["spec"]


def test_cpu_limit_is_refused():
    err = render_fails("--set", "resources.limits.cpu=500m")
    assert "schema" in err.lower() or "limits.cpu" in err


# ── PDB must not deadlock node drains ───────────────────────────────────────
def test_no_pdb_for_a_single_replica():
    assert "PodDisruptionBudget" not in kinds(render())


def test_pdb_rendered_for_multiple_replicas():
    assert "PodDisruptionBudget" in kinds(render("--set", "replicas=3", *SHARED))


def test_pdb_that_can_never_be_satisfied_is_refused():
    err = render_fails("--set", "podDisruptionBudget.minAvailable=2")
    assert "PDB DEADLOCK" in err


# ── the internal tier and metrics must never be public ──────────────────────
@pytest.mark.parametrize("path", ["/", "/internal", "/internal/analytics/ingest", "/metrics"])
def test_ingress_refuses_to_expose_non_user_paths(path):
    err = render_fails("--set", "ingress.enabled=true", "--set", "ingress.host=x.io",
                       "--set", f"ingress.paths[0]={path}")
    assert "REFUSING TO EXPOSE" in err


def test_ingress_allows_the_user_tier():
    out = render("--set", "ingress.enabled=true", "--set", "ingress.host=x.io")
    ing = find(out, "Ingress")
    assert ing["spec"]["rules"][0]["http"]["paths"][0]["path"] == "/api/v1"


def test_public_ingress_without_network_policy_is_refused():
    err = render_fails("--set", "ingress.enabled=true", "--set", "ingress.host=x.io",
                       "--set", "networkPolicy.enabled=false")
    assert "networkPolicy.enabled=false" in err


# ── network policy shape ────────────────────────────────────────────────────
def test_default_deny_policy_is_always_rendered():
    policies = [d for d in docs(render()) if d["kind"] == "NetworkPolicy"]
    deny = [p for p in policies if p["metadata"]["name"].endswith("-default-deny")]
    assert deny, "no default-deny NetworkPolicy"
    spec = deny[0]["spec"]
    assert spec["ingress"] == [] and spec["egress"] == []
    assert set(spec["policyTypes"]) == {"Ingress", "Egress"}


def test_internet_egress_is_off_by_default():
    assert "0.0.0.0/0" not in render()


def test_internet_egress_still_excludes_private_and_metadata_ranges():
    out = render("--set", "networkPolicy.egress.internet=true")
    policies = [d for d in docs(out) if d["kind"] == "NetworkPolicy"]
    allow = [p for p in policies if p["metadata"]["name"].endswith("-allow")][0]
    block = next(r["to"][0]["ipBlock"] for r in allow["spec"]["egress"]
                 if r.get("to") and "ipBlock" in r["to"][0])
    assert block["cidr"] == "0.0.0.0/0"
    for cidr in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16"):
        assert cidr in block["except"], f"{cidr} reachable — SSRF can pivot to the cluster"


# ── image + hardening ───────────────────────────────────────────────────────
def test_image_is_resolved_by_digest_never_by_tag():
    dep = find(render(), "Deployment")
    image = dep["spec"]["template"]["spec"]["containers"][0]["image"]
    assert "@sha256:" in image and ":latest" not in image


def test_a_missing_digest_is_refused():
    err = render_fails("--set", "image.digest=")
    assert "schema" in err.lower()


def test_pod_hardening_is_not_optional():
    dep = find(render(), "Deployment")
    spec = dep["spec"]["template"]["spec"]
    c = spec["containers"][0]
    assert spec["automountServiceAccountToken"] is False
    assert spec["securityContext"]["runAsNonRoot"] is True
    assert spec["securityContext"]["seccompProfile"]["type"] == "RuntimeDefault"
    assert c["securityContext"]["readOnlyRootFilesystem"] is True
    assert c["securityContext"]["allowPrivilegeEscalation"] is False
    assert c["securityContext"]["capabilities"]["drop"] == ["ALL"]


def test_readonly_rootfs_has_a_writable_tmp():
    """readOnlyRootFilesystem without /tmp crash-loops on the first temp write."""
    dep = find(render(), "Deployment")
    mounts = dep["spec"]["template"]["spec"]["containers"][0]["volumeMounts"]
    assert any(m["mountPath"] == "/tmp" for m in mounts)


def test_single_replica_rollout_replaces_rather_than_doubles():
    """A single-replica service REPLACES on rollout (maxSurge:0), it does not surge a second pod.

    A surge pod opens a SECOND postgres connection pool while the old pod still holds its own; on a
    fleet sharing one postgres that transient doubling is a rollout trigger for connection
    exhaustion (and with maxSurge the old pod is not retired until the new one is Ready — a
    deadlock). maxSurge:0 retires the old pod first, freeing its connections. The service is briefly
    unavailable, which a single-replica service is on any restart anyway."""
    dep = find(render(), "Deployment")            # default is one replica
    ru = dep["spec"]["strategy"]["rollingUpdate"]
    assert ru["maxSurge"] == 0 and ru["maxUnavailable"] == 1
    spec = dep["spec"]["template"]["spec"]
    assert spec["terminationGracePeriodSeconds"] >= 1
    # preStop pause: endpoint removal is async, so without it every deploy serves 502s.
    assert "preStop" in spec["containers"][0]["lifecycle"]


def test_multi_replica_rollout_keeps_capacity():
    """With >1 replica, other replicas keep serving, so the rollout never drops below the current
    count (maxUnavailable:0) and surges one at a time (maxSurge:1) — the classic zero-downtime shape,
    which is safe here because the connection doubling is bounded to one extra pod, not a whole
    second copy of a lone service."""
    dep = find(render("--set", "replicas=3", *SHARED), "Deployment")
    ru = dep["spec"]["strategy"]["rollingUpdate"]
    assert ru["maxUnavailable"] == 0 and ru["maxSurge"] == 1


def test_config_change_rolls_the_pods():
    dep = find(render(), "Deployment")
    assert "checksum/config" in dep["spec"]["template"]["metadata"]["annotations"]


# ── cronjobs: the endpoints that otherwise never run ────────────────────────
def test_cronjobs_require_a_secret_for_the_internal_token():
    err = render_fails("--set", "cronJobs[0].name=p", "--set", "cronJobs[0].schedule=* * * * *",
                       "--set", "cronJobs[0].path=/internal/x")
    assert "existingSecret" in err


def test_cronjob_path_outside_the_internal_tier_is_refused():
    err = render_fails("--set", "existingSecret=s", "--set", "cronJobs[0].name=p",
                       "--set", "cronJobs[0].schedule=* * * * *",
                       "--set", "cronJobs[0].path=/api/v1/x")
    assert "schema" in err.lower()


def test_cronjob_never_overlaps_itself():
    out = render("--set", "existingSecret=s", "--set", "cronJobs[0].name=p",
                 "--set", "cronJobs[0].schedule=* * * * *",
                 "--set", "cronJobs[0].path=/internal/x")
    cj = find(out, "CronJob")
    assert cj["spec"]["concurrencyPolicy"] == "Forbid"
    assert cj["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["image"].count("@sha256:")


# ── an unknown values key is a typo, not a feature ──────────────────────────
def test_unknown_values_key_is_refused():
    err = render_fails("--set", "replicaz=3")
    assert "schema" in err.lower()


# ══════════════════════════════════════════════════════════════════════════
# THE BIG ONE: every env name the chart injects must be a real Settings field.
# Kubernetes happily sets an env var nobody reads: `MAX_GROUP` for `MAX_GROUPS`
# deploys clean, is silently ignored, and surfaces as a wrong number.
# ══════════════════════════════════════════════════════════════════════════
def settings_fields(service: str) -> set[str]:
    cfg = (SERVICES_DIR / service / "app" / "config.py").read_text(encoding="utf-8")
    # Field declarations inside the Settings class: `    NAME: type = default`
    return set(re.findall(r"^\s{4}([A-Z][A-Z0-9_]*)\s*:", cfg, re.MULTILINE))


def service_values() -> list[pathlib.Path]:
    return sorted(VALUES_DIR.glob("*.values.yaml"))


def test_every_service_has_a_values_file():
    have = {p.name.replace(".values.yaml", "") for p in service_values()}
    want = {p.name for p in SERVICES_DIR.iterdir()
            if p.is_dir() and p.name != "_template" and (p / "app").is_dir()}
    assert have == want, f"missing deploy values for: {want - have}"


@pytest.mark.parametrize("vf", service_values(), ids=lambda p: p.name)
def test_every_chart_env_name_exists_in_that_services_settings(vf):
    service = vf.name.replace(".values.yaml", "")
    fields = settings_fields(service)
    assert fields, f"could not parse Settings for {service}"

    out = render(values_file=vf)
    injected: set[str] = set()

    cm = find(out, "ConfigMap")
    if cm:
        injected |= set(cm.get("data", {}))

    dep = find(out, "Deployment")
    for e in dep["spec"]["template"]["spec"]["containers"][0].get("env", []):
        injected.add(e["name"])

    unknown = injected - fields
    assert not unknown, (
        f"{service}: chart sets env vars that are NOT fields on its Settings: "
        f"{sorted(unknown)} — Kubernetes will set them and the app will ignore them"
    )


@pytest.mark.parametrize("vf", service_values(), ids=lambda p: p.name)
def test_values_service_name_matches_settings_service_name(vf):
    service = vf.name.replace(".values.yaml", "")
    values = yaml.safe_load(vf.read_text(encoding="utf-8"))
    assert values["service"]["name"] == service


@pytest.mark.parametrize("vf", service_values(), ids=lambda p: p.name)
def test_every_service_values_file_renders(vf):
    assert "Deployment" in kinds(render(values_file=vf))


def test_only_the_gateway_is_publicly_exposed():
    exposed = []
    for vf in service_values():
        values = yaml.safe_load(vf.read_text(encoding="utf-8"))
        if values.get("ingress", {}).get("enabled"):
            exposed.append(vf.name.replace(".values.yaml", ""))
    assert exposed == ["api-gateway"], f"unexpectedly public: {exposed}"


def test_internet_egress_is_granted_only_to_services_that_call_out():
    allowed = set()
    for vf in service_values():
        values = yaml.safe_load(vf.read_text(encoding="utf-8"))
        if values.get("networkPolicy", {}).get("egress", {}).get("internet"):
            allowed.add(vf.name.replace(".values.yaml", ""))
    assert allowed == {"webhook-service", "connector-service", "notification-service",
                       "billing-service", "zatca-service"}, allowed


def test_the_maintenance_endpoints_we_shipped_have_a_scheduled_caller():
    """We built these endpoints; without a CronJob nothing ever calls them."""
    expected = {
        "analytics-service": "/internal/analytics/prune-raw",
        "audit-service": "/internal/audit/enforce-retention",
        "webhook-service": "/internal/webhooks/sweep",
        "zatca-service": "/internal/zatca/sweep",
        "billing-service": "/internal/billing/dunning",
        "job-service": "/internal/scheduler/tick",
        # Without this one, a hold never expires and a dead client strands a customer's
        # credits permanently — the exact leak wallet-service was built to close.
        "wallet-service": "/internal/wallet/sweep-holds",
    }
    for service, path in expected.items():
        values = yaml.safe_load((VALUES_DIR / f"{service}.values.yaml").read_text(encoding="utf-8"))
        paths = {c["path"] for c in values.get("cronJobs", [])}
        assert path in paths, f"{service}: {path} has no scheduled caller"


# ══════════════════════════════════════════════════════════════════════════
# The mutation guard — the deploy-time twin of each service's config-knob test.
#
# A values key that changes nothing is a knob with no consumer: it looks
# configurable, is documented, and does nothing. Mutating a key must either
# change the rendered output, or make the render FAIL (a guard is a consumer).
#
# Each key is exercised inside the context that ENABLES its feature — testing
# `ingress.host` with ingress disabled would only prove the `if` works.
# ══════════════════════════════════════════════════════════════════════════
_HPA = SHARED + ["--set", "autoscaling.enabled=true"]
_ING = ["--set", "ingress.enabled=true", "--set", "ingress.host=x.io"]
_SM = ["--set", "metrics.serviceMonitor=true"]
_OBJ = ["--set", "networkPolicy.egress.objectStore.enabled=true"]  # port/roleLabel only render when on
_MULTI = SHARED + ["--set", "replicas=3"]
_CRON = ["--set", "existingSecret=s", "--set", "cronJobs[0].name=p",
         "--set", "cronJobs[0].schedule=* * * * *", "--set", "cronJobs[0].path=/internal/x"]

# (values key, enabling context, mutated value)
MUTATIONS = [
    ("replicas", SHARED, "3"),
    ("service.port", [], "8081"),
    ("service.name", [], "other-service"),
    ("image.repository", [], "other/repo"),
    ("image.digest", [], "sha256:" + "b" * 64),
    ("image.pullPolicy", [], "Always"),
    ("drivers.datastore", [], "postgres"),
    ("drivers.cache", [], "redis"),
    ("drivers.eventbus", [], "redis"),
    ("drivers.queue", [], "redis"),
    ("drivers.ratelimiter", [], "redis"),
    ("resources.requests.cpu", [], "250m"),
    ("resources.requests.memory", [], "999Mi"),
    ("resources.limits.memory", [], "999Mi"),
    ("autoscaling.enabled", SHARED, "true"),
    ("autoscaling.minReplicas", _HPA, "4"),
    ("autoscaling.maxReplicas", _HPA, "9"),
    ("autoscaling.targetCPUUtilizationPercentage", _HPA, "55"),
    ("autoscaling.scaleDownStabilizationSeconds", _HPA, "60"),
    ("podDisruptionBudget.enabled", _MULTI, "false"),
    ("podDisruptionBudget.minAvailable", _MULTI, "2"),
    ("probes.startupFailureThreshold", [], "42"),
    ("probes.periodSeconds", [], "11"),
    ("probes.timeoutSeconds", [], "9"),
    ("probes.livenessFailureThreshold", [], "8"),
    ("terminationGracePeriodSeconds", [], "45"),
    ("preStopSleepSeconds", [], "9"),
    ("ingress.enabled", ["--set", "ingress.host=x.io"], "true"),
    ("ingress.className", _ING, "nginx"),
    ("ingress.host", _ING, "y.io"),
    ("ingress.tls.enabled", _ING, "false"),
    ("ingress.tls.secretName", _ING, "my-tls"),
    ("networkPolicy.enabled", [], "false"),
    ("networkPolicy.ingressFromNamespace", [], "ingress"),
    ("networkPolicy.egress.dns", [], "false"),
    ("networkPolicy.egress.datastore", [], "false"),
    ("networkPolicy.egress.objectStore.enabled", [], "true"),
    ("networkPolicy.egress.objectStore.port", _OBJ, "9999"),
    ("networkPolicy.egress.objectStore.roleLabel", _OBJ, "minio"),
    ("networkPolicy.egress.calicoFqdn.enabled", [], "true"),
    ("networkPolicy.egress.internet", [], "true"),
    ("metrics.serviceMonitor", [], "true"),
    ("metrics.interval", _SM, "60s"),
    ("existingSecret", [], "my-secret"),
    ("podAntiAffinity", [], "hard"),
    ("topologySpread", [], "false"),
    ("priorityClassName", [], "high"),
    ("nameOverride", [], "renamed"),
    ("fullnameOverride", [], "renamed-full"),
    ("cronJobs[0].schedule", _CRON, "5 5 * * *"),
    # A CronJob's zone is read from the deployment timeZone: mutating it changes spec.timeZone. Uses
    # a real IANA zone because the template REJECTS an unknown one (that rejection is itself a consumer).
    ("timeZone", _CRON, "UTC"),
]


@pytest.mark.parametrize("key,ctx,mutated", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_every_values_key_has_a_consumer(key, ctx, mutated):
    cmd = [HELM, "template", "t", str(CHART)] + BASE + list(ctx)
    before = subprocess.run(cmd, capture_output=True, text=True)
    assert before.returncode == 0, f"context render failed for {key}: {before.stderr[:200]}"

    after = subprocess.run(cmd + ["--set", f"{key}={mutated}"], capture_output=True, text=True)
    if after.returncode != 0:
        return  # a guard rejected it — that IS a consumer

    assert after.stdout != before.stdout, (
        f"values key {key!r} changed nothing when set to {mutated!r}: it is documented, "
        f"configurable, and inert. Wire it, or delete it."
    )


def test_every_values_key_is_covered_by_the_mutation_guard():
    """Stop a new knob from being added without proving it does something."""
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))

    def leaves(node, prefix=""):
        for k, v in (node or {}).items():
            path = f"{prefix}.{k}" if prefix else k
            if isinstance(v, dict):
                yield from leaves(v, path)
            elif isinstance(v, list):
                continue           # list-valued knobs (paths, cronJobs) covered explicitly
            else:
                yield path

    declared = set(leaves(values))
    tested = {m[0] for m in MUTATIONS}
    uncovered = declared - tested
    assert not uncovered, f"values keys with no mutation test: {sorted(uncovered)}"


def test_image_has_no_inert_tag_knob():
    """`tag` would be read by nothing: the image is always resolved by digest."""
    values = yaml.safe_load((CHART / "values.yaml").read_text(encoding="utf-8"))
    assert "tag" not in values["image"]


def test_values_schema_rejects_unknown_keys_everywhere():
    schema = json.loads((CHART / "values.schema.json").read_text(encoding="utf-8"))

    def check(node, where="root"):
        if isinstance(node, dict):
            if node.get("type") == "object" and "properties" in node:
                assert node.get("additionalProperties") is False, f"{where} allows unknown keys"
            for k, v in node.get("properties", {}).items():
                check(v, f"{where}.{k}")

    check(schema)


def test_every_pod_spawning_kind_carries_the_pull_secret():
    """A pull secret on the Deployment but not the CronJob is a private-registry outage that
    passes every test.

    The CronJob pulls the SAME image as the Deployment. Omit `imagePullSecrets` there and it works
    perfectly on a public registry and fails on every private one — and it fails on the SCHEDULER
    TICK, not at deploy time. So `helm install` goes green, the pods are Running, and the job
    quietly never runs. Nobody finds out until the thing the job was supposed to do did not happen.

    So the rule is not "the deployment has it". It is: ANY kind that spawns a pod has it.
    """
    rendered = render(
        "--set", "image.pullSecrets={regcred}",
        # The chart refuses cronJobs without a secret to carry INTERNAL_SERVICE_TOKEN — its own
        # guard, and a correct one. Satisfy it so we can test the pull secret.
        "--set", "existingSecret=app-secrets",
        "--set", "cronJobs[0].name=tick",
        "--set", "cronJobs[0].schedule=*/5 * * * *",
        "--set", "cronJobs[0].path=/internal/tick",
    )
    pod_kinds = {"Deployment", "CronJob", "Job", "StatefulSet", "DaemonSet"}
    seen = {}
    for d in docs(rendered):
        if not d or d.get("kind") not in pod_kinds:
            continue
        # Walk to the podSpec wherever this kind happens to keep it.
        spec = d["spec"]
        for hop in ("jobTemplate", "template"):
            while hop in spec:
                spec = spec[hop]["spec"] if "spec" in spec[hop] else spec[hop]
        names = [s.get("name") for s in (spec.get("imagePullSecrets") or [])]
        seen[d["kind"]] = names

    assert "CronJob" in seen, "the CronJob did not render — this guard would be vacuous"
    for kind, names in seen.items():
        assert names == ["regcred"], (
            f"{kind} does not carry image.pullSecrets — it cannot pull from a private registry, "
            f"and on a CronJob that failure only appears on the scheduler tick"
        )
