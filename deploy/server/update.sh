#!/usr/bin/env bash
# ==================================================================================================
#  base-services — health-gated rollout: roll the running platform to its pinned digests
# --------------------------------------------------------------------------------------------------
#  A SAFE replacement for `make deploy`'s bare upgrade loop. Same idea — one generic chart, one values
#  file per service — but every service is rendered before the cluster is touched, waited on for
#  health, and AUTO-ROLLED-BACK to its previous revision if it does not come up. A release's identity
#  is the set of digests in the values directory; immutable by construction, so a rollback is exact.
#
#  PLUG-AND-PLAY. Nothing here is base-services-specific except the defaults. A product that consumes
#  this platform points the three env vars at its own tree and gets the same safe rollout:
#      PRODUCT_NAME=raje3 CHART=platform/deploy/chart VALUES_DIR=deploy/services bash update.sh
#
#  Usage:
#    sudo bash deploy/server/update.sh                    # roll every service to its pinned digest
#    sudo bash deploy/server/update.sh --only tenant,audit  # roll ONLY these; leave the rest untouched
#    sudo bash deploy/server/update.sh --dry-run          # render everything, change nothing
#    sudo bash deploy/server/update.sh --no-rollback      # leave a failed release up for debugging
#
#  Env:  PRODUCT_NAME (banner, default base-services) · NS (default base-services) ·
#        CHART (default <repo>/deploy/chart) · VALUES_DIR (default <repo>/deploy/pinned, else
#        deploy/services) · HEALTH_TIMEOUT (default 360)
# ==================================================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"                 # repo root (deploy/server/ -> ../..)
PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
NS="${NS:-${NAMESPACE:-base-services}}"           # NS or NAMESPACE (the Makefile uses NAMESPACE)
CHART="${CHART:-$ROOT/deploy/chart}"
# Prefer the digest-pinned release values; fall back to the source values (placeholder digests) so a
# fresh checkout that has not cut a release yet still rolls.
if [[ -z "${VALUES_DIR:-}" ]]; then
  if compgen -G "$ROOT/deploy/pinned/*.values.yaml" >/dev/null; then VALUES_DIR="$ROOT/deploy/pinned";
  else VALUES_DIR="$ROOT/deploy/services"; fi
fi
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-360}"           # helm --wait: a first install pulls an image first
ROLLBACK=1
DRY_RUN=0

C_G=$'\e[32m'; C_C=$'\e[36m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_N=$'\e[0m'; C_D=$'\e[2m'
info(){ echo -e "${C_C}▸ $*${C_N}"; }
ok(){   echo -e "${C_G}  ✔ $*${C_N}"; }
warn(){ echo -e "${C_Y}  ⚠ $*${C_N}"; }
die(){  echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }

ONLY=""      # optional CSV of service names to roll — the rest are LEFT UNTOUCHED
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-rollback) ROLLBACK=0;;
    --dry-run) DRY_RUN=1;;
    --only) shift; ONLY="${1:-}"; [[ -n "$ONLY" ]] || die "--only needs a comma-separated list";;
    --only=*) ONLY="${1#--only=}";;
    -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
    -*) die "unknown flag $1";;
    *) die "unexpected argument '$1' — this rolls the digests in $VALUES_DIR, it takes no tag";;
  esac; shift
done

# Roll only the named services (and tolerate a bare name or a "-service" suffix). Re-rolling every
# service to change one is slow and restarts pods nobody touched, so --only exists.
_in_only() {
  [[ -z "$ONLY" ]] && return 0
  local n="$1"; IFS=',' read -ra want <<< "$ONLY"
  for w in "${want[@]}"; do
    w="$(echo "$w" | xargs)"
    [[ "$n" == "$w" || "$n" == "$w-service" || "$n-service" == "$w" ]] && return 0
  done
  return 1
}

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
command -v helm >/dev/null || die "helm missing (deploy/server/provision-server.sh installs it)"
kubectl get nodes >/dev/null 2>&1 || die "kubectl cannot reach the cluster (is k3s up?)"
# One clear line instead of N identical helm errors if the tree is wrong or a checkout is half-done.
[[ -f "$CHART/Chart.yaml" ]] || die "chart not found at $CHART — run from the repo, or set CHART=<path>"

echo -e "${C_C}━━━  ${PRODUCT_NAME} — rolling the platform to its pinned digests  ━━━${C_N}"
echo -e "  ${C_D}chart=$CHART${C_N}"
echo -e "  ${C_D}values=$VALUES_DIR${C_N}"
[[ $DRY_RUN -eq 1 ]] && warn "DRY RUN — every service is rendered, nothing is applied"

mapfile -t targets < <(ls "$VALUES_DIR"/*.values.yaml 2>/dev/null | xargs -r -n1 basename | sed "s/\.values\.yaml$//")
[[ ${#targets[@]} -gt 0 ]] || die "no values files in $VALUES_DIR"

[[ -n "$ONLY" ]] && info "rolling ONLY: ${ONLY} (all other services left untouched)" \
                  || info "rolling ${#targets[@]} services"

updated=0 failed=0 skipped=0
for svc in "${targets[@]}"; do
  if ! _in_only "$svc"; then skipped=$((skipped+1)); continue; fi
  values="$VALUES_DIR/$svc.values.yaml"
  printf "  %-26s ... " "$svc"

  # RENDER FIRST. A values file that cannot render is caught BEFORE the cluster is touched, rather
  # than halfway through with some services on the new release and some on the old.
  if ! helm template "$svc" "$CHART" -f "$values" >/dev/null 2>&1; then
    echo -e "${C_R}render failed${C_N}"
    helm template "$svc" "$CHART" -f "$values" 2>&1 | tail -3 | sed "s/^/      /"
    failed=$((failed+1)); continue
  fi
  if [[ $DRY_RUN -eq 1 ]]; then echo -e "${C_D}renders clean${C_N}"; updated=$((updated+1)); continue; fi

  # The revision to return to, read BEFORE the upgrade. `helm rollback <n>` to a known revision beats
  # a bare `rollback`, which trusts helm's idea of "previous" after a failed release.
  prev="$(helm -n "$NS" status "$svc" -o json 2>/dev/null | sed -n 's/.*"revision":[ ]*\([0-9]*\).*/\1/p' | head -1)"

  if helm upgrade --install "$svc" "$CHART" -f "$values" \
       --namespace "$NS" --create-namespace --wait --timeout "${HEALTH_TIMEOUT}s" >/dev/null 2>&1; then
    echo -e "${C_G}ok${C_N}"; updated=$((updated+1))
  else
    echo -e "${C_R}failed${C_N}"; failed=$((failed+1))
    kubectl -n "$NS" logs "deploy/$svc" --tail=5 2>/dev/null | sed "s/^/      /" || true
    if [[ $ROLLBACK -eq 1 && -n "${prev:-}" && "${prev:-0}" -gt 0 ]]; then
      warn "$svc did not become healthy — rolling back to revision $prev"
      helm -n "$NS" rollback "$svc" "$prev" --wait --timeout "${HEALTH_TIMEOUT}s" >/dev/null 2>&1 \
        && ok "$svc rolled back" \
        || warn "$svc rollback also had issues — inspect: helm -n $NS history $svc"
    elif [[ $ROLLBACK -eq 0 ]]; then
      warn "$svc left in its failed state (--no-rollback)"
    else
      warn "$svc has no previous revision (first install) — nothing to roll back to"
    fi
  fi
done

echo -e "\n${C_G}━━━  update summary ━━━${C_N}"
echo -e "  source:     ${C_C}${VALUES_DIR}/*.values.yaml${C_N}"
echo -e "  updated:    ${C_G}${updated}${C_N} / ${#targets[@]}"
[[ $skipped -gt 0 ]] && echo -e "  skipped:    ${C_C}${skipped}${C_N} (not in --only; left untouched)"
[[ $failed -gt 0 ]] && echo -e "  failed:     ${C_R}${failed}${C_N}$([[ $ROLLBACK -eq 1 ]] && echo ' (rolled back)')"
kubectl -n "$NS" get deploy --no-headers 2>/dev/null | awk '{printf "  %-26s %s\n",$1,$2}'
echo -e "\n  Not-ready pods (if any):"
# Fully-ready+Running, or a Completed job pod, is fine; anything else is worth showing. (mawk has no
# regex backreference, so split READY on "/" rather than matching ^(n)/\1$, which over-lists healthy pods.)
kubectl -n "$NS" get pods --no-headers 2>/dev/null \
  | awk '{split($2,a,"/"); ok=(a[1]==a[2] && $3=="Running"); if(!(ok || $3=="Completed")) print "    "$0}' \
  | head -20 || true
[[ $failed -eq 0 ]] || exit 1

# PRUNE superseded node images now that every deployment is on the new digest and healthy — the old
# tags are no longer referenced by any pod, which is what slowly fills a long-lived box. Only on a
# fully-successful rollout (a rollback might still need the previous image). Best-effort.
if command -v crictl >/dev/null 2>&1; then
  crictl rmi --prune >/dev/null 2>&1 && echo -e "  ${C_G}✔ pruned superseded node images${C_N}" || true
elif command -v k3s >/dev/null 2>&1; then
  k3s crictl rmi --prune >/dev/null 2>&1 && echo -e "  ${C_G}✔ pruned superseded node images${C_N}" || true
fi

echo -e "\n${C_G}✔ ${PRODUCT_NAME} rolled — every service on its pinned digest${C_N}"
