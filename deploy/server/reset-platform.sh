#!/usr/bin/env bash
# ==================================================================================================
#  base-services — reset-platform.sh
#  A POWERFUL, IDEMPOTENT datastore reset + verify. It exists because of one trap that kept biting a
#  real reinstall by hand:
#
#    * Deleting a PVC does NOT wipe the k3s local-path HOST directory. So a "fresh" postgres reuses
#      the stale data dir, SKIPS first-time init, and never creates the `base_services` database —
#      every service then sits 0/1 on a datastore that looks up but has no schema. This script deletes
#      the PVC AND its host directory, verifies the directory is gone, and ASSERTS `base_services`
#      exists after boot (creating it directly if postgres auto-init did not).
#
#  Usage:
#    sudo bash deploy/server/reset-platform.sh              # wipe datastore data, re-bring-up, verify
#    sudo bash deploy/server/reset-platform.sh --no-wipe    # keep data; reconcile secrets + roll + verify
#    sudo bash deploy/server/reset-platform.sh --no-secrets # do not (re)mint secrets
#    sudo bash deploy/server/reset-platform.sh --verify-only # only the end checks
#
#  Env: PRODUCT_NAME · NAMESPACE (default base-services) · STORAGE (default /var/lib/rancher/k3s/storage)
#       DB_NAME (default base_services). Redis holds no PVC (ephemeral); only postgres/objectstore data
#       is wiped.
# ==================================================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
PRODUCT_NAME="${PRODUCT_NAME:-base-services}"
NS="${NAMESPACE:-${NS:-base-services}}"
STORAGE="${STORAGE:-/var/lib/rancher/k3s/storage}"
DB_NAME="${DB_NAME:-base_services}"
WIPE=1; SECRETS=1; VERIFY_ONLY=0
for a in "$@"; do case "$a" in
  --no-wipe) WIPE=0;; --no-secrets) SECRETS=0;; --verify-only) VERIFY_ONLY=1;;
  -h|--help) grep -E '^#( |$)' "$0" | sed 's/^# \{0,1\}//'; exit 0;;
  *) echo "unknown flag $a" >&2; exit 2;; esac; done

C_G=$'\e[32m'; C_C=$'\e[36m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_B=$'\e[1m'; C_N=$'\e[0m'
phase(){ echo -e "\n${C_B}${C_C}══════ $* ══════${C_N}"; }
info(){ echo -e "${C_C}  ▸ $*${C_N}"; }
ok(){   echo -e "${C_G}✔ $*${C_N}"; }
warn(){ echo -e "${C_Y}⚠ $*${C_N}"; }
die(){  echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }
K="kubectl -n $NS"
retry(){ local n="$1" s="$2"; shift 2; local i; for i in $(seq 1 "$n"); do "$@" && return 0; sleep "$s"; done; return 1; }

[[ $EUID -eq 0 ]] || die "run as root: sudo bash $0"
export KUBECONFIG="${KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
command -v kubectl >/dev/null || die "kubectl not found (run provision-server.sh)"
kubectl get nodes >/dev/null 2>&1 || die "kubectl cannot reach the cluster (is k3s up?)"

# psql inside the postgres pod, using the pod's own POSTGRES_* env (from platform-secrets).
pgx(){ $K exec postgres-0 -- sh -c "PGPASSWORD=\$POSTGRES_PASSWORD psql -U \$POSTGRES_USER -d ${1:-postgres} -tAc \"$2\"" 2>/dev/null; }
db_exists(){ pgx postgres "select 1 from pg_database where datname='$DB_NAME'" | grep -q 1; }
create_db(){ $K exec postgres-0 -- sh -c "PGPASSWORD=\$POSTGRES_PASSWORD createdb -U \$POSTGRES_USER $DB_NAME" >/dev/null 2>&1; db_exists; }
pg_ready(){ $K exec postgres-0 -- pg_isready -U postgres >/dev/null 2>&1; }

echo -e "${C_B}${C_C}━━━  ${PRODUCT_NAME} — platform reset (ns=${NS})  ━━━${C_N}"

if [[ $VERIFY_ONLY -eq 0 ]]; then
  # ── 1 · wipe datastore data (PVC AND host dir — the part a human kept doing by hand) ──────────────
  if [[ $WIPE -eq 1 ]]; then
    phase "1 · wipe datastore data (postgres + objectstore) — PVC AND host dir"
    $K delete statefulset postgres --ignore-not-found --wait=true --timeout=90s >/dev/null 2>&1 || true
    $K delete deployment objectstore --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1 || true
    for pvc in postgres-data objectstore-data; do
      $K get pvc "$pvc" >/dev/null 2>&1 || continue
      info "deleting PVC $pvc + its host directory"
      $K delete pvc "$pvc" --timeout=60s >/dev/null 2>&1 || true
      # local-path stores at $STORAGE/pvc-<uid>_<ns>_<pvc>; wipe every dir carrying the pvc name.
      for d in $(find "$STORAGE" -maxdepth 1 -type d -iname "*_${NS}_${pvc}" 2>/dev/null) \
               $(find "$STORAGE" -maxdepth 1 -type d -iname "*${pvc}*" 2>/dev/null); do
        rm -rf "$d" && ok "removed $d"
      done
    done
    # REFUSE to continue if any survived — a leftover dir is exactly what makes postgres skip init.
    if find "$STORAGE" -maxdepth 1 -type d 2>/dev/null | grep -qiE "_${NS}_(postgres-data|objectstore-data)$"; then
      die "a datastore host dir survived the wipe — refuse to continue (would skip init)."
    fi
    ok "datastore host directories clean"
  else
    ok "--no-wipe — datastore data left intact"
  fi

  # ── 2 · bring the datastores back up ─────────────────────────────────────────────────────────────
  phase "2 · bootstrap datastores"
  bash "$ROOT/deploy/k3s/bootstrap.sh" || die "bootstrap failed"
  info "waiting for postgres to accept connections"
  retry 30 5 pg_ready || die "postgres did not become ready — inspect: $K logs postgres-0"
  ok "postgres ready"

  # ── 3 · ASSERT the shared database exists (do NOT trust auto-init after a host-dir reuse) ─────────
  phase "3 · assert database '$DB_NAME'"
  if ! db_exists; then
    warn "$DB_NAME missing — creating it directly"
    retry 5 5 create_db || die "could not create $DB_NAME — inspect: $K logs postgres-0"
  fi
  ok "$DB_NAME present"

  # ── 4 · secrets ──────────────────────────────────────────────────────────────────────────────────
  if [[ $SECRETS -eq 1 ]]; then
    phase "4 · secrets (gen-secrets.py — preserves existing values)"
    [ -f "$HERE/gen-secrets.py" ] && python3 "$HERE/gen-secrets.py" || warn "gen-secrets.py not present — skipping"
  fi

  # ── 5 · roll the services ────────────────────────────────────────────────────────────────────────
  phase "5 · roll services (server/update.sh)"
  NAMESPACE="$NS" PRODUCT_NAME="$PRODUCT_NAME" bash "$HERE/update.sh" || warn "some services did not roll cleanly (see above)"
fi

# ── 6 · verify ──────────────────────────────────────────────────────────────────────────────────────
phase "6 · verify"
db_exists && ok "database '$DB_NAME' exists" || warn "database '$DB_NAME' NOT found"
info "deployment readiness:"
$K get deploy --no-headers 2>/dev/null | awk '{printf "    %-26s %s\n",$1,$2}'
notready="$($K get pods --no-headers 2>/dev/null | awk '{split($2,a,"/"); if(!((a[1]==a[2]&&$3=="Running")||$3=="Completed")) print "    "$0}')"
if [[ -n "$notready" ]]; then warn "not-ready pods:"; echo "$notready" | head -20; else ok "all pods ready"; fi
echo
ok "${C_B}reset complete${C_N}"
