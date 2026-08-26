#!/usr/bin/env bash
# PgBouncer cutover — flip services from talking to Postgres directly to going through PgBouncer,
# safely and reversibly, one canary first. See deploy/k3s/platform/pgbouncer.yaml for the why.
#
#   deploy/k3s/pgbouncer-cutover.sh verify                 # prove auth THROUGH pgbouncer works
#   deploy/k3s/pgbouncer-cutover.sh canary <service>       # repoint ONE service, restart it
#   deploy/k3s/pgbouncer-cutover.sh rollout                # repoint every remaining service
#   deploy/k3s/pgbouncer-cutover.sh rollback <service>     # revert one service
#   deploy/k3s/pgbouncer-cutover.sh rollback --all         # revert everything
#   deploy/k3s/pgbouncer-cutover.sh status                 # who is on pgbouncer vs direct
#   deploy/k3s/pgbouncer-cutover.sh pg-conns               # live Postgres client-backend count
#
# Add `--wait-under N` to canary/rollout to GATE each cutover on Postgres headroom — it blocks until
# client backends < N before repointing the next service, so a rollout never piles onto a saturated
# server during the transition window (when direct + pooled connections coexist). E.g. --wait-under 85.
#
# Env: NAMESPACE (default base-services). The cutover rewrites each service's own <svc>-secrets
# DATASTORE_DSN: host postgres:5432 -> pgbouncer:6432 and sslmode=require -> disable (the pod->pgbouncer
# hop is in-cluster + NetworkPolicy-restricted; pgbouncer->Postgres stays TLS). The pre-cutover DSN is
# stashed in a secret annotation so rollback is exact, never a guess.
set -euo pipefail

NS="${NAMESPACE:-base-services}"
FROM_HOST="postgres:5432"
TO_HOST="pgbouncer:6432"
ANNOT="pgbouncer-cutover/original-dsn"

die() { echo "ERROR: $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
have kubectl || die "kubectl not found"

_b64d() { base64 -d 2>/dev/null || base64 -D; }          # GNU vs BSD base64
_b64e() { base64 -w0 2>/dev/null || base64;  }

# Every service secret that actually carries a DATASTORE_DSN (so this never touches a non-DB service).
_db_secrets() {
  kubectl -n "$NS" get secrets -o json \
    | python3 -c 'import json,sys;[print(s["metadata"]["name"]) for s in json.load(sys.stdin)["items"] if "DATASTORE_DSN" in (s.get("data") or {})]'
}
_svc_of() { echo "${1%-secrets}"; }                       # <svc>-secrets -> <svc>
_dsn() { kubectl -n "$NS" get secret "$1" -o jsonpath='{.data.DATASTORE_DSN}' | _b64d; }

_on_pgbouncer() { case "$(_dsn "$1")" in *"$TO_HOST"*) return 0;; *) return 1;; esac; }

# Live count of CLIENT backends on Postgres — the number that must stay under the ~100 ceiling. During
# a partial rollout BOTH the still-direct services and pgbouncer's climbing server pool count here, so
# this is what the --wait-under gate watches (the end state, all behind pgbouncer, is bounded; only the
# coexistence window is tight). Run against the postgres pod over its local trust socket.
_pg_pod() { kubectl -n "$NS" get pod -l app=postgres -o jsonpath='{.items[0].metadata.name}' 2>/dev/null; }
pg_conns() {
  local user pod
  user="$(kubectl -n "$NS" get secret platform-secrets -o jsonpath='{.data.POSTGRES_USER}' | _b64d)"
  pod="$(_pg_pod)"; [ -n "$pod" ] || die "no postgres pod found (label app=postgres) in $NS"
  kubectl -n "$NS" exec "$pod" -- psql -U "$user" -d base_services -tAc \
    "select count(*) from pg_stat_activity where backend_type='client backend'" | tr -d '[:space:]'
}

# Block until Postgres client backends are UNDER the ceiling before repointing the next service, so a
# cutover never piles onto an already-saturated server. Bounded wait (~2min) then fail loudly.
WAIT_UNDER=""
_gate() {
  [ -n "$WAIT_UNDER" ] || return 0
  local n tries=0
  while :; do
    n="$(pg_conns)"; [ -n "$n" ] || die "could not read pg_stat_activity"
    if [ "$n" -lt "$WAIT_UNDER" ]; then echo "   [gate] pg client backends=$n (< $WAIT_UNDER) — go"; return 0; fi
    tries=$((tries + 1))
    [ "$tries" -le 60 ] || die "pg client backends stuck at $n (>= $WAIT_UNDER) after ~2min — investigate the ceiling before continuing the rollout"
    echo "   [gate] pg client backends=$n (>= $WAIT_UNDER) — waiting for headroom..."; sleep 2
  done
}

verify() {
  echo "==> checking pgbouncer is Ready..."
  kubectl -n "$NS" rollout status deployment/pgbouncer --timeout=60s
  local user pass
  user="$(kubectl -n "$NS" get secret platform-secrets -o jsonpath='{.data.POSTGRES_USER}' | _b64d)"
  pass="$(kubectl -n "$NS" get secret platform-secrets -o jsonpath='{.data.POSTGRES_PASSWORD}' | _b64d)"
  echo "==> connecting THROUGH pgbouncer (scram auth + SELECT 1)..."
  # A throwaway client pod; the digest-pinned postgres image already carries psql.
  kubectl -n "$NS" run pgbouncer-verify --rm -i --restart=Never --quiet \
    --image="postgres:16.4-alpine" --env="PGPASSWORD=$pass" -- \
    psql "host=pgbouncer port=6432 dbname=base_services user=$user sslmode=disable" \
    -tAc "select 'pgbouncer-ok ' || version()" \
    || die "could not authenticate/select THROUGH pgbouncer — do NOT cut over. Check pgbouncer logs
       (kubectl -n $NS logs deploy/pgbouncer): a scram mismatch or SERVER_TLS_SSLMODE is the usual cause."
  echo "==> OK — pgbouncer authenticates and serves queries. Safe to canary."
}

_repoint() {  # <secret> — rewrite its DSN to go through pgbouncer, stashing the original for rollback
  local secret="$1" dsn new
  dsn="$(_dsn "$secret")"
  case "$dsn" in *"$TO_HOST"*) echo "   $secret already on pgbouncer — skipped"; return 0;; esac
  _gate                                   # wait for Postgres headroom before adding another migration
  new="$(printf '%s' "$dsn" | sed -E "s#@${FROM_HOST}#@${TO_HOST}#g; s#sslmode=require#sslmode=disable#g")"
  [ "$new" != "$dsn" ] || die "$secret DSN did not match '$FROM_HOST' — refusing a no-op rewrite (is it already migrated, or a different host?)"
  kubectl -n "$NS" annotate secret "$secret" "$ANNOT=$dsn" --overwrite >/dev/null
  kubectl -n "$NS" patch secret "$secret" --type merge -p \
    "{\"data\":{\"DATASTORE_DSN\":\"$(printf '%s' "$new" | _b64e)\"}}" >/dev/null
  kubectl -n "$NS" rollout restart "deployment/$(_svc_of "$secret")" >/dev/null
  echo "   $(_svc_of "$secret") -> pgbouncer (restarting)"
}

_revert() {  # <secret> — restore the stashed pre-cutover DSN
  local secret="$1" orig
  orig="$(kubectl -n "$NS" get secret "$secret" -o "jsonpath={.metadata.annotations.${ANNOT//./\\.}}" 2>/dev/null || true)"
  [ -n "$orig" ] || { echo "   $(_svc_of "$secret"): no stashed original — skipped"; return 0; }
  kubectl -n "$NS" patch secret "$secret" --type merge -p \
    "{\"data\":{\"DATASTORE_DSN\":\"$(printf '%s' "$orig" | _b64e)\"}}" >/dev/null
  kubectl -n "$NS" annotate secret "$secret" "${ANNOT}-" >/dev/null 2>&1 || true
  kubectl -n "$NS" rollout restart "deployment/$(_svc_of "$secret")" >/dev/null
  echo "   $(_svc_of "$secret") -> postgres (reverted, restarting)"
}

# Pull an optional `--wait-under N` out of the args (applies to canary/rollout); the rest stays positional.
_args=()
while [ $# -gt 0 ]; do
  case "$1" in
    --wait-under) WAIT_UNDER="${2:?--wait-under needs a number}"; shift 2 ;;
    *) _args+=("$1"); shift ;;
  esac
done
set -- "${_args[@]:-}"

case "${1:-}" in
  verify) verify ;;
  pg-conns) echo "postgres client backends: $(pg_conns)" ;;
  canary)
    [ -n "${2:-}" ] || die "usage: canary <service>"
    verify
    echo "==> canary: repointing $2 only"
    _repoint "$2-secrets"
    echo "==> watch it, then re-run your load test against $2. When happy: rollout. If not: rollback $2"
    ;;
  rollout)
    verify
    echo "==> repointing every remaining DB service"
    for s in $(_db_secrets); do _repoint "$s"; done
    ;;
  rollback)
    [ -n "${2:-}" ] || die "usage: rollback <service> | rollback --all"
    if [ "$2" = "--all" ]; then for s in $(_db_secrets); do _revert "$s"; done
    else _revert "$2-secrets"; fi
    ;;
  status)
    for s in $(_db_secrets); do
      if _on_pgbouncer "$s"; then echo "  pgbouncer  $(_svc_of "$s")"; else echo "  direct     $(_svc_of "$s")"; fi
    done ;;
  *) die "usage: $0 {verify | canary <svc> | rollout | rollback <svc>|--all | status | pg-conns} [--wait-under N]" ;;
esac
