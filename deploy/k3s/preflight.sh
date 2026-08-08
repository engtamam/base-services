#!/usr/bin/env bash
# =============================================================================
# Preflight — PROVE the cluster enforces what the chart promises.
#
# Checking that a Calico pod exists proves it was installed. It does not prove
# that this NetworkPolicy blocks this packet. So we send the packet.
#
# The control matters: a connection can fail because the policy worked, or
# because the cluster's networking is broken. A naive test cannot tell those
# apart and reports "enforced" for a dead cluster. So we prove, in order:
#
#     1. baseline  — client CAN reach server            (networking works)
#     2. deny      — apply default-deny, client CANNOT  (policy is enforced)
#     3. restore   — remove policy, client CAN again    (we broke nothing)
#
# Only 1-then-2-then-3 proves enforcement. Anything else fails loud.
# =============================================================================
set -euo pipefail

NS="${PREFLIGHT_NS:-base-services-preflight}"
TIMEOUT="${TIMEOUT:-90s}"
# busybox is used only as a TCP client/server inside a throwaway namespace.
IMAGE="${PREFLIGHT_IMAGE:-busybox:1.36}"

info() { printf '\033[1;34m[ .. ]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; cleanup; exit 1; }

cleanup() { kubectl delete namespace "$NS" --ignore-not-found --wait=false >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Can the client open a TCP connection to the server? 0 = yes, 1 = no.
can_connect() {
  kubectl -n "$NS" run client-$RANDOM --rm -i --restart=Never --image="$IMAGE" \
    --command --timeout="$TIMEOUT" -- \
    sh -c 'nc -w 3 -z server 8080 && echo REACHABLE || echo BLOCKED' 2>/dev/null \
    | grep -q REACHABLE
}

# Retry can_connect for up to ~60s. `wait --for=condition=Ready pod/server` means the container is
# RUNNING — not that `nc -l` is listening yet, nor that the Service endpoint is programmed in the
# dataplane. A single probe races that window and reports a false "networking is broken"; the CNI
# was enforcing correctly the whole time. So we poll, and only a persistent failure is real.
wait_reachable() {
  local tries=0
  until can_connect; do
    tries=$((tries + 1))
    [ "$tries" -ge 12 ] && return 1
    sleep 5
  done
  return 0
}

# Blocking delete so a re-run does not race its own previous cleanup. The EXIT trap deletes with
# --wait=false (fast exit); if that namespace is still Terminating when the next run starts,
# `kubectl create namespace` dies with AlreadyExists ("object is being deleted") and every re-run
# then fails for a different reason than the first. Wait for it to be fully gone before recreating.
ensure_ns_absent() {
  kubectl delete namespace "$NS" --ignore-not-found --wait=true --timeout="$TIMEOUT" >/dev/null 2>&1 || true
  local tries=0
  while kubectl get namespace "$NS" >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -ge 24 ] && die "namespace $NS is stuck Terminating; delete it by hand and retry"
    sleep 5
  done
}

setup() {
  ensure_ns_absent
  kubectl create namespace "$NS" >/dev/null
  kubectl label namespace "$NS" "kubernetes.io/metadata.name=$NS" --overwrite >/dev/null
  info "starting a server pod in $NS"
  kubectl -n "$NS" run server --image="$IMAGE" --labels="app=server" \
    --port=8080 --command -- sh -c 'while true; do echo ok | nc -l -p 8080; done' >/dev/null
  kubectl -n "$NS" expose pod server --port=8080 --target-port=8080 >/dev/null
  kubectl -n "$NS" wait --for=condition=Ready pod/server --timeout="$TIMEOUT" >/dev/null
}

deny_all() {
  kubectl -n "$NS" apply -f - >/dev/null <<'YAML'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: preflight-default-deny
spec:
  podSelector:
    matchLabels:
      app: server
  policyTypes: [Ingress]
  ingress: []
YAML
  # Give the CNI a moment to program the dataplane.
  sleep 5
}

# ── 1. NetworkPolicy is really enforced ─────────────────────────────────────
check_network_policy() {
  info "network policy: proving the control path BEFORE trusting the deny"
  setup

  wait_reachable \
    || die "baseline connectivity FAILED after ~60s of retries: the client cannot reach the server
       even with no policy. The cluster's networking is broken — a later 'blocked' result would
       prove nothing."
  ok "baseline: client reaches server (networking works)"

  info "applying default-deny and re-testing"
  deny_all
  if can_connect; then
    die "NETWORK POLICY IS NOT ENFORCED. A default-deny policy is applied and the client
       still reaches the server. Your CNI ignores NetworkPolicy (stock K3s ships flannel,
       which does not implement it). Every deny-by-default in this platform is decorative.
       Fix: run deploy/k3s/bootstrap.sh, which installs Calico."
  fi
  ok "deny-by-default is ENFORCED (connection refused)"

  kubectl -n "$NS" delete networkpolicy preflight-default-deny >/dev/null
  wait_reachable || die "policy removed but the client still cannot connect — the cluster is
       in a bad state; the 'enforced' result above may be a networking failure in disguise."
  ok "restore: connectivity returns once the policy is removed"

  cleanup
}

# ── 2. The metrics API answers (or every HPA is inert) ──────────────────────
check_metrics_server() {
  info "metrics API: an HPA without it reports <unknown> and never scales"
  kubectl get apiservice v1beta1.metrics.k8s.io >/dev/null 2>&1 \
    || die "metrics.k8s.io API not registered — install metrics-server (bootstrap.sh does)."

  local tries=0
  until kubectl top nodes >/dev/null 2>&1; do
    tries=$((tries + 1))
    [ "$tries" -ge 12 ] && die "metrics API registered but returns no data after 60s.
       Every HorizontalPodAutoscaler will sit at minReplicas with a green rollout."
    sleep 5
  done
  ok "metrics API serves node metrics (HPAs can compute utilization)"
}

# ── 3. Namespace labels the chart's policies select on ──────────────────────
check_namespace_labels() {
  local ns="${NAMESPACE:-base-services}"
  kubectl get namespace "$ns" >/dev/null 2>&1 || { info "namespace $ns absent; skipping"; return; }
  kubectl get namespace "$ns" -o jsonpath='{.metadata.labels.kubernetes\.io/metadata\.name}' \
    | grep -q "$ns" \
    || die "namespace $ns lacks the kubernetes.io/metadata.name label the chart's
       NetworkPolicy namespaceSelectors match on — ingress/monitoring rules would never match."
  ok "namespace labels present"
}

main() {
  command -v kubectl >/dev/null || die "kubectl not found"
  kubectl version >/dev/null 2>&1 || die "cannot reach the cluster"
  echo "============================================================"
  echo "  Preflight — proving, not assuming"
  echo "============================================================"
  check_network_policy
  check_metrics_server
  check_namespace_labels
  echo "============================================================"
  ok "PREFLIGHT PASS — the platform's guarantees are enforced here"
}

main "$@"
