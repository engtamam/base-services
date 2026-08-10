#!/usr/bin/env bash
# ==================================================================================================
#  verify-postgres-tls.sh — does postgres ACTUALLY serve TLS, on the wire?
#
#  Byte-level, not status-line. It is not enough that datastores.yaml SAYS ssl=on: a cert with the
#  wrong permissions, a missing Secret, or a typo makes postgres log a warning and start WITHOUT
#  SSL — cleartext, green rollout, nobody the wiser. So this makes a REAL TCP connection with
#  sslmode=require (which refuses to proceed unless the server negotiates TLS) and then reads the
#  session's negotiated state from pg_stat_ssl — the server's own account of what it encrypted,
#  not what it was configured to do.
#
#  This is the SERVER half's gate. It must pass BEFORE gen-secrets.py is allowed to append
#  sslmode=require to any service DSN — because that is the exact condition (server serves TLS) the
#  client half depends on to not fail every service at boot.
#
#  Exit 0 = the wire is encrypted. Non-zero = it is not (or postgres is unreachable) — a hard stop.
#
#  Env: NS/NAMESPACE (default base-services) · POSTGRES_SELECTOR (default app=postgres)
# ==================================================================================================
set -Eeuo pipefail
NS="${NS:-${NAMESPACE:-base-services}}"
POSTGRES_SELECTOR="${POSTGRES_SELECTOR:-app=postgres}"

C_G=$'\e[32m'; C_C=$'\e[36m'; C_R=$'\e[31m'; C_N=$'\e[0m'; C_D=$'\e[2m'
info(){ echo -e "${C_C}▸ $*${C_N}"; }
ok(){   echo -e "${C_G}  ✔ $*${C_N}"; }
die(){  echo -e "${C_R}✖ $*${C_N}" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "kubectl not found"
pod="$(kubectl -n "$NS" get pods -l "$POSTGRES_SELECTOR" -o name 2>/dev/null | head -1 || true)"
[[ -n "$pod" ]] || die "no postgres pod (selector '$POSTGRES_SELECTOR' in ns '$NS')"

info "verifying postgres serves TLS on the wire ($pod)"

# Connect over TCP to the loopback interface with sslmode=require. Two things must both hold:
#   1. psql connects at all — with sslmode=require it would ERROR if the server offered no TLS.
#   2. pg_stat_ssl reports ssl=t for THIS backend — the negotiated truth, read from the server.
# The socket path is deliberately host=127.0.0.1 (TCP), never the unix socket, which never uses SSL.
res="$(kubectl -n "$NS" exec "$pod" -- sh -c '
  psql "host=127.0.0.1 port=5432 user=${POSTGRES_USER:-postgres} dbname=${POSTGRES_DB:-base_services} sslmode=require" \
       -tAc "SELECT ssl FROM pg_stat_ssl WHERE pid = pg_backend_pid()"
' 2>&1 | tr -d '[:space:]' || true)"

case "$res" in
  t) ok "TLS negotiated on the wire (pg_stat_ssl.ssl = t, via sslmode=require)"
     echo -e "  ${C_D}the server half is in place — gen-secrets.py may now append sslmode=require to DSNs${C_N}"
     ;;
  f) die "postgres accepted a connection but did NOT encrypt it (pg_stat_ssl.ssl = f). ssl=on likely
      failed silently — check the cert/key permissions and that the postgres-tls Secret is mounted." ;;
  *) die "could not confirm TLS (server refused sslmode=require, or postgres is not serving TLS).
      Raw result: ${res:-<empty>}. Do NOT let gen-secrets add sslmode=require until this passes." ;;
esac
