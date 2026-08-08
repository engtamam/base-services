#!/usr/bin/env bash
# ==================================================================================================
#  preflight-db-pool.sh — will the fleet's connection pools fit inside postgres?
#
#  The arithmetic that turns a 2am production deadlock into a one-line refusal at install time.
#  Every service opens a postgres pool. min is a STICKY floor: once a service is used, it holds
#  min_size connections for its lifetime. So the fleet's GUARANTEED demand is services × pool_min,
#  and its PEAK is services × pool_max, against postgres max_connections (minus a reserve for
#  superuser/replication). If the floor alone exceeds the ceiling, the fleet WILL deadlock — not
#  at startup, but later, on an ordinary request, once enough distinct services have been touched.
#
#  FATAL when the floor does not fit (a certain deadlock). A WARNING when the peak does not fit
#  (likely under load) — the standard answer there is pgbouncer in transaction mode, a higher
#  max_connections, a smaller pool_max, or a database per service.
#
#  Env (all optional; sane defaults so it runs anywhere):
#    NS/NAMESPACE          k8s namespace (default base-services) — to auto-read max_connections
#    VALUES_DIR            where the per-service values live (default deploy/pinned, else deploy/services)
#    POOL_MIN / POOL_MAX   the pool sizing the fleet deploys with (default 1 / 5 — the code defaults)
#    PG_MAX_CONNECTIONS    override the ceiling if auto-detect cannot reach postgres
#    PG_RESERVED           connections to leave for superuser/replication/psql (default 5)
#    POSTGRES_SELECTOR     label selector for the postgres pod (default app=postgres)
# ==================================================================================================
set -Eeuo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
NS="${NS:-${NAMESPACE:-base-services}}"
POOL_MIN="${POOL_MIN:-1}"
POOL_MAX="${POOL_MAX:-5}"
PG_RESERVED="${PG_RESERVED:-5}"
POSTGRES_SELECTOR="${POSTGRES_SELECTOR:-app=postgres}"

C_G=$'\e[32m'; C_C=$'\e[36m'; C_Y=$'\e[33m'; C_R=$'\e[31m'; C_N=$'\e[0m'; C_D=$'\e[2m'
info(){ echo -e "${C_C}▸ $*${C_N}"; }
ok(){   echo -e "${C_G}  ✔ $*${C_N}"; }
warn(){ echo -e "${C_Y}  ⚠ $*${C_N}"; }
die(){  echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }

# How many services will deploy — one values file each.
if [[ -z "${VALUES_DIR:-}" ]]; then
  if compgen -G "$ROOT/deploy/pinned/*.values.yaml" >/dev/null; then VALUES_DIR="$ROOT/deploy/pinned";
  else VALUES_DIR="$ROOT/deploy/services"; fi
fi
SERVICES="$(ls "$VALUES_DIR"/*.values.yaml 2>/dev/null | wc -l | tr -d ' ')"
[[ "$SERVICES" -gt 0 ]] || die "no values files in $VALUES_DIR — cannot count services"

# The ceiling: read it from the live postgres if we can reach it; otherwise assume the stock 100 and
# say so LOUDLY, because a wrong ceiling is the whole thing this check exists to get right.
MAXCONN="${PG_MAX_CONNECTIONS:-}"
SRC="PG_MAX_CONNECTIONS override"
if [[ -z "$MAXCONN" ]]; then
  pod="$(kubectl -n "$NS" get pods -l "$POSTGRES_SELECTOR" -o name 2>/dev/null | head -1 || true)"
  if [[ -n "$pod" ]]; then
    MAXCONN="$(kubectl -n "$NS" exec "$pod" -- sh -c 'psql -U "${POSTGRES_USER:-postgres}" -tAc "SHOW max_connections"' 2>/dev/null | tr -dc '0-9' || true)"
    [[ -n "$MAXCONN" ]] && SRC="live postgres ($pod)"
  fi
fi
if [[ -z "$MAXCONN" ]]; then
  MAXCONN=100; SRC="ASSUMED stock default (could not reach postgres — set PG_MAX_CONNECTIONS to be sure)"
fi

floor=$(( SERVICES * POOL_MIN ))
peak=$(( SERVICES * POOL_MAX ))
usable=$(( MAXCONN - PG_RESERVED ))

info "db-pool preflight"
echo -e "  ${C_D}services=$SERVICES · pool_min=$POOL_MIN · pool_max=$POOL_MAX · max_connections=$MAXCONN ($SRC) · reserved=$PG_RESERVED${C_N}"
echo -e "  ${C_D}guaranteed floor = $SERVICES×$POOL_MIN = $floor · peak = $SERVICES×$POOL_MAX = $peak · usable = $usable${C_N}"

if (( floor > usable )); then
  die "the fleet's GUARANTEED floor ($floor) exceeds usable connections ($usable). This WILL deadlock
      once enough services are touched — not at startup, later, on an ordinary request. Fix before
      deploying: put pgbouncer (transaction mode) in front of postgres, raise max_connections, lower
      DATASTORE_POOL_MIN, or give heavy services their own database."
fi
if (( peak > usable )); then
  warn "the fleet's PEAK demand ($peak) exceeds usable connections ($usable). It fits at rest but can
      exhaust under load or during a surge rollout. Recommended: pgbouncer (transaction mode) so
      generous pools share a small backend count, OR raise max_connections, OR lower DATASTORE_POOL_MAX."
  exit 0
fi
ok "pools fit: floor $floor and peak $peak both within usable $usable"
