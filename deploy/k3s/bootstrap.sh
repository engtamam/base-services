#!/usr/bin/env bash
# =============================================================================
# K3s bootstrap — a cluster our chart's promises are actually true on.
#
# Two of those promises need cluster-level support, and BOTH fail silently if it
# is missing:
#   * NetworkPolicy needs a policy-capable CNI. Stock K3s ships flannel, which
#     does NOT enforce NetworkPolicy — the objects apply cleanly and traffic
#     fails OPEN. So we disable flannel's policy path and install Calico.
#   * The HPA needs metrics-server. Without it the HPA reports <unknown>/70%
#     and never scales, with a green rollout.
#
# `preflight.sh` proves both AFTER install. Installing is not the same as working.
# =============================================================================
set -euo pipefail

NAMESPACE="${NAMESPACE:-base-services}"
CALICO_VERSION="${CALICO_VERSION:-v3.28.2}"
K3S_CHANNEL="${K3S_CHANNEL:-stable}"

info() { printf '\033[1;34m[ .. ]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ OK ]\033[0m %s\n' "$*"; }
die()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; exit 1; }

need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1"; }

# ── 1. K3s with a policy-capable CNI ────────────────────────────────────────
install_k3s() {
  if command -v k3s >/dev/null 2>&1; then
    ok "k3s already installed"
    return
  fi
  need curl
  info "installing k3s (flannel disabled; Calico will provide the CNI)"
  # --flannel-backend=none + --disable-network-policy: we bring our own policy CNI.
  # Without this, NetworkPolicy objects are inert and every deny-by-default is theatre.
  curl -sfL https://get.k3s.io | INSTALL_K3S_CHANNEL="$K3S_CHANNEL" sh -s - \
    --flannel-backend=none \
    --disable-network-policy \
    --disable traefik \
    --write-kubeconfig-mode 644
  ok "k3s installed"
}

# ── 2. Calico ───────────────────────────────────────────────────────────────
install_calico() {
  if kubectl get pods -n kube-system -o name 2>/dev/null | grep -qi calico; then
    ok "calico present"
    return
  fi
  info "installing Calico $CALICO_VERSION"
  kubectl apply -f "https://raw.githubusercontent.com/projectcalico/calico/${CALICO_VERSION}/manifests/calico.yaml"
  kubectl -n kube-system rollout status daemonset/calico-node --timeout=300s
  ok "calico ready"
}

# ── 3. metrics-server (the HPA is inert without it) ─────────────────────────
install_metrics_server() {
  if kubectl get deploy -n kube-system metrics-server >/dev/null 2>&1; then
    ok "metrics-server present"
    return
  fi
  info "installing metrics-server"
  kubectl apply -f https://github.com/kubernetes-sigs/metrics-server/releases/latest/download/components.yaml
  kubectl -n kube-system rollout status deploy/metrics-server --timeout=300s
  ok "metrics-server ready"
}

# ── Postgres TLS cert: generated HERE, on the host, at provision time ───────
# WHY on the host and not in an initContainer: the postgres pod runs under a default-deny egress
# NetworkPolicy, so an initContainer that `apk add openssl` would hang forever reaching the Alpine
# CDN. openssl is already installed by provision-server.sh, so we mint the cert here and ship it as
# a Secret the StatefulSet mounts. Self-signed is sufficient for sslmode=require (encrypt, don't
# verify identity) — the realistic pod-network threat is passive interception.
#
# IDEMPOTENT and STICKY: if the Secret already exists we leave it untouched, so a re-run neither
# rotates the cert (which would bounce postgres for no reason) nor depends on openssl being present
# on a later run. Delete the Secret to force a fresh cert.
ensure_postgres_tls() {
  if kubectl -n "$NAMESPACE" get secret postgres-tls >/dev/null 2>&1; then
    ok "postgres TLS cert present (kept)"
    return
  fi
  command -v openssl >/dev/null 2>&1 || die "openssl not found — needed to mint the postgres TLS cert (provision-server.sh installs it)"
  info "minting self-signed postgres TLS cert"
  local dir; dir="$(mktemp -d)"
  # SANs cover the in-cluster names, so this same cert works unchanged if a future deployment
  # tightens to verify-full — no re-mint, no CA needed today.
  openssl req -new -x509 -nodes -days 3650 \
    -subj "/CN=postgres" \
    -addext "subjectAltName=DNS:postgres,DNS:postgres.${NAMESPACE}.svc,DNS:postgres.${NAMESPACE}.svc.cluster.local" \
    -keyout "$dir/tls.key" -out "$dir/tls.crt" >/dev/null 2>&1 \
    || die "openssl failed to generate the postgres TLS cert"
  kubectl -n "$NAMESPACE" create secret tls postgres-tls \
    --cert="$dir/tls.crt" --key="$dir/tls.key" >/dev/null \
    || die "failed to create the postgres-tls Secret"
  rm -rf "$dir"
  ok "postgres TLS cert minted"
}

# ── 4. Platform: ingress, certs, sealed secrets, data stores ────────────────
install_platform() {
  kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -
  # The chart's NetworkPolicy selects Prometheus/ingress by namespace LABEL, and
  # kubernetes.io/metadata.name is only auto-set on 1.21+. Assert rather than assume.
  kubectl label namespace "$NAMESPACE" "kubernetes.io/metadata.name=$NAMESPACE" --overwrite >/dev/null

  ensure_postgres_tls

  info "applying platform manifests"
  # -n "$NAMESPACE": the platform manifests are namespace-agnostic (no metadata.namespace), so
  # without this the datastores land in `default` while every charted service looks for
  # postgres.$NAMESPACE.svc — a green bootstrap with nothing reachable. Pin them to the namespace
  # we created and labelled above.
  kubectl apply -n "$NAMESPACE" -f "$(dirname "$0")/platform/"
  ok "platform applied"

  # OPTIONAL in-cluster object store (SeaweedFS, Apache-2.0), for OBJECTSTORE_DRIVER=s3. Off by
  # default — most deployments bring their own S3. It needs the `objectstore-secrets` Secret
  # (s3.json); see deploy/k3s/objectstore.yaml. Bring-your-own S3? Leave this off.
  if [ -n "${OBJECTSTORE_ENABLED:-}" ]; then
    if kubectl -n "$NAMESPACE" get secret objectstore-secrets >/dev/null 2>&1; then
      info "applying optional object store (SeaweedFS)"
      kubectl apply -n "$NAMESPACE" -f "$(dirname "$0")/objectstore.yaml"
      ok "object store applied"
    else
      die "OBJECTSTORE_ENABLED is set but the 'objectstore-secrets' Secret (s3.json) is missing —
       create it first (see deploy/k3s/objectstore.yaml), or unset OBJECTSTORE_ENABLED to bring
       your own S3."
    fi
  fi
}

main() {
  need kubectl
  install_k3s
  export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
  install_calico
  install_metrics_server
  install_platform
  echo
  ok "bootstrap complete — now run: deploy/k3s/preflight.sh"
  echo "   (bootstrap only INSTALLS. preflight PROVES.)"
}

main "$@"
