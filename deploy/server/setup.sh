#!/usr/bin/env bash
# ==================================================================================================
#  base-services — setup.sh · THE ONE COMMAND
#  Stand up (or re-stand-up) the WHOLE platform on a server, in order, from a single entrypoint.
#  No hand-run steps, no remembering the order.
#
#      sudo bash deploy/server/setup.sh
#
#  Phases (each idempotent; an already-done phase is a fast no-op):
#    1. provision-server.sh   the host: packages, hardening, firewall, k3s   (optional — see below)
#    2. k3s/bootstrap.sh       Calico + metrics-server + datastores
#    3. k3s/preflight.sh       PROVE default-deny is enforced — FATAL if not
#    4. pull-images.sh         registry credential for PRIVATE images         (optional)
#    5. hooks/pre-deploy.sh    e.g. secrets, DB roles/migrations              (optional hook)
#    6. server/update.sh       render-first, health-gated rollout + rollback  (every values file)
#    7. hooks/post-deploy.sh   e.g. seed data                                 (optional hook)
#    8. boot-heal.sh           survive an ungraceful reboot                   (optional)
#
#  PLUG-AND-PLAY. Optional phases run ONLY if their script exists, so a product drops in just what it
#  needs (a secrets generator, a DB provisioner, a seeder) under deploy/server/ or deploy/hooks/ and
#  the orchestrator picks them up — without editing this file.
#
#  Phase 3 is FATAL: a cluster that cannot enforce NetworkPolicy is not a slow start, it is a
#  tenant-isolation hole — the one guarantee the platform cannot ship without.
#
#  Env: PRODUCT_NAME (banner) · NAMESPACE (default base-services) · SKIP_PROVISION=1 (host ready) ·
#       DRY_RUN=1 (print the plan, change nothing)
# ==================================================================================================
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"                 # repo root (deploy/server/ -> ../..)
DEPLOY="$ROOT/deploy"
HOOKS="$DEPLOY/hooks"
PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
NAMESPACE="${NAMESPACE:-base-services}"
DRY_RUN="${DRY_RUN:-0}"

C_G=$'\e[32m'; C_C=$'\e[36m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_D=$'\e[2m'; C_B=$'\e[1m'; C_N=$'\e[0m'
phase(){ echo -e "\n${C_B}${C_C}══════ $* ══════${C_N}"; }
ok(){    echo -e "${C_G}✔ $*${C_N}"; }
warn(){  echo -e "${C_Y}⚠ $*${C_N}"; }
skip(){  echo -e "${C_D}▸ skip — $*${C_N}"; }
die(){   echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }
run(){   if [ "$DRY_RUN" = "1" ]; then echo -e "${C_D}  [dry-run] $*${C_N}"; else "$@"; fi; }

# Run an OPTIONAL script if it exists; otherwise say so and move on. This is the plug-and-play seam.
run_optional(){ # <label> <path> [args...]
  local label="$1" path="$2"; shift 2
  if [ -f "$path" ]; then run bash "$path" "$@"; else skip "$label ($(basename "$path") not present)"; fi
}

[ "$(id -u)" = "0" ] || die "run as root: sudo bash $0"
[ "$DRY_RUN" = "1" ] && warn "DRY RUN — nothing will be modified"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

echo -e "${C_B}${C_C}━━━  ${PRODUCT_NAME} — one-command setup  ━━━${C_N}"

# ── 1 · the host ─────────────────────────────────────────────────────────────────────────────────
phase "1 · provision the host"
if [ "${SKIP_PROVISION:-0}" = "1" ]; then
  ok "SKIP_PROVISION=1 — leaving the host alone"
elif kubectl get nodes >/dev/null 2>&1; then
  ok "k3s already answering — skipping provision-server.sh"
else
  run_optional "host provisioning" "$HERE/provision-server.sh"
fi

# ── 2 · the cluster ──────────────────────────────────────────────────────────────────────────────
phase "2 · bootstrap the cluster (Calico + metrics-server + datastores)"
[ -f "$DEPLOY/k3s/bootstrap.sh" ] || die "deploy/k3s/bootstrap.sh missing — run from the repo root"
run bash "$DEPLOY/k3s/bootstrap.sh" || die "bootstrap failed"

# ── 3 · PROVE it, do not assume it ───────────────────────────────────────────────────────────────
# bootstrap INSTALLS Calico. preflight sends an actual packet and proves the policy drops it.
# Presence is not enforcement: a NetworkPolicy on a flannel cluster applies cleanly and fails OPEN.
phase "3 · preflight — prove default-deny is enforced"
[ -f "$DEPLOY/k3s/preflight.sh" ] || die "deploy/k3s/preflight.sh missing"
if [ "$DRY_RUN" = "1" ]; then
  echo -e "${C_D}  [dry-run] bash $DEPLOY/k3s/preflight.sh${C_N}"
elif bash "$DEPLOY/k3s/preflight.sh"; then
  ok "cluster enforces what the chart promises"
else
  die "preflight FAILED — this cluster does not enforce NetworkPolicy. Do not deploy onto it.
     Most likely cause: k3s installed with stock flannel. Fix:
       k3s-uninstall.sh && sudo bash $HERE/provision-server.sh"
fi

# ── 3b · db-pool preflight — will the fleet's connection pools fit inside postgres? ────────────────
# FATAL only when the guaranteed floor exceeds the ceiling (a certain deadlock, later, on an ordinary
# request). It warns (non-fatal) when only the peak does. A no-op if the script is absent.
phase "3b · db-pool preflight — do the connection pools fit in postgres?"
if [ "$DRY_RUN" = "1" ]; then
  echo -e "${C_D}  [dry-run] bash $HERE/preflight-db-pool.sh${C_N}"
else
  run_optional "db-pool preflight" "$HERE/preflight-db-pool.sh"
fi

# ── 4 · registry credential (private images only) ────────────────────────────────────────────────
phase "4 · image pull-secret for private images"
run_optional "pull-secret" "$HERE/pull-images.sh" --no-prewarm

# ── 5 · pre-deploy hook (secrets, DB roles/migrations) ───────────────────────────────────────────
phase "5 · pre-deploy hook"
run_optional "pre-deploy" "$HOOKS/pre-deploy.sh"

# ── 6 · deploy the services (render-first, health-gated, auto-rollback) ──────────────────────────
phase "6 · roll out services (server/update.sh)"
[ -f "$HERE/update.sh" ] || die "server/update.sh missing"
if [ "$DRY_RUN" = "1" ]; then
  run bash "$HERE/update.sh" --dry-run || die "rollout render failed"
else
  NAMESPACE="$NAMESPACE" PRODUCT_NAME="$PRODUCT_NAME" bash "$HERE/update.sh" \
    || die "rollout failed — nothing after this point is trustworthy"
fi

# ── 7 · post-deploy hook (seed data) ─────────────────────────────────────────────────────────────
phase "7 · post-deploy hook"
run_optional "post-deploy" "$HOOKS/post-deploy.sh"

# ── 8 · reboot resilience ────────────────────────────────────────────────────────────────────────
phase "8 · survive an ungraceful reboot"
run_optional "boot-heal" "$HERE/boot-heal.sh" --install
# Graceful node shutdown is cheap and independent of boot-heal: a clean reboot drains pods instead of
# stranding them. Applies on the next k3s restart.
if [ "$DRY_RUN" != "1" ]; then
  mkdir -p /etc/rancher/k3s
  if ! grep -q "shutdown-grace-period" /etc/rancher/k3s/config.yaml 2>/dev/null; then
    printf 'kubelet-arg:\n  - "shutdown-grace-period=30s"\n  - "shutdown-grace-period-critical-pods=10s"\n' \
      >> /etc/rancher/k3s/config.yaml
    ok "k3s graceful-shutdown armed (applies on next k3s restart)"
  fi
fi

echo
ok "${C_B}setup complete${C_N}"
kubectl -n "$NAMESPACE" get deploy 2>/dev/null || true
