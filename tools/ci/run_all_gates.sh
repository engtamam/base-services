#!/usr/bin/env bash
# =============================================================================
# run_all_gates.sh — the SINGLE SOURCE OF TRUTH for the merge-blocking gates.
#
# CI calls this. You call this. `local == CI` is the whole point: a gate that
# only exists inside a YAML file is a gate nobody can run before pushing.
#
# Three outcomes, never two:
#     PASS   the gate ran and held
#     FAIL   the gate ran and caught something   -> merge blocked
#     SKIP   the gate is WIRED but its infra is absent (loud, listed, and in
#            --strict mode it becomes a FAIL)
#
# A gate that silently passes because its tool was missing is worse than no gate.
# CI runs with --strict, so "we installed postgres/helm" is PROVEN, not assumed.
#
# Nothing here is hardcoded to a service list: services are DISCOVERED, so a new
# service is gated the moment it exists.
#
# Usage:
#   tools/ci/run_all_gates.sh                 # everything runnable here
#   tools/ci/run_all_gates.sh --strict        # a SKIP is a FAIL (what CI runs)
#   tools/ci/run_all_gates.sh --profile quick # fast pre-commit lane
#   tools/ci/run_all_gates.sh --list          # every gate + what it protects
#   tools/ci/run_all_gates.sh --only chart:guards
#
# Env:
#   DATASTORE_TEST_DSN   run the DataStore contract against a REAL postgres
#   HELM                 path to helm (default: on PATH)
#   GATE_SEED            primary PYTHONHASHSEED (default 0)
#   GATE_SEED2           second seed for the determinism gate (default 1337)
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

PY_DEFAULT="services/_template/.venv/Scripts/python.exe"
[ -x "$PY_DEFAULT" ] || PY_DEFAULT="services/_template/.venv/bin/python"
[ -x "$PY_DEFAULT" ] || PY_DEFAULT="python"
PY="${PYTHON:-$PY_DEFAULT}"
HELM="${HELM:-helm}"
PRIMARY_SEED="${GATE_SEED:-0}"
SECOND_SEED="${GATE_SEED2:-1337}"

STRICT=0
PROFILE="${GATE_PROFILE:-all}"
ONLY=""

FAILED=(); SKIPPED=(); PASSED=()

banner() { printf '\n\033[1m=== %s ===\033[0m\n%s\n' "$1" "$2"; }
ok()     { printf '\033[32m  PASS\033[0m  %s\n' "$1"; PASSED+=("$1"); }
fail()   { printf '\033[31m  FAIL\033[0m  %s\n' "$1"; FAILED+=("$1"); }
skip()   { printf '\033[33m  SKIP (WIRED)\033[0m  %s — %s\n' "$1" "$2"; SKIPPED+=("$1: $2"); }
noprof() { printf '\033[36m  N/A\033[0m   %s — not in the %s profile\n' "$1" "$2"; }
have()   { command -v "$1" >/dev/null 2>&1; }
# With --only, a gate we are not running must not print its banner: the output
# should describe what actually ran, not what could have.
want()   { [ -z "$ONLY" ] || [ "$ONLY" = "$1" ]; }

# ── argument parsing ────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --strict)  STRICT=1; shift ;;
    --profile) PROFILE="${2:?--profile needs quick|all}"; shift 2 ;;
    --only)    ONLY="${2:?--only needs a gate name}"; shift 2 ;;
    --list)    PROFILE="__list"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# ── PROFILES ────────────────────────────────────────────────────────────────
# A SPLIT, never a weakening: every gate runs in `all` (the pre-merge default
# and what CI runs). `quick` drops only the slow lanes, and prints a loud N/A
# for each one it drops, so nobody mistakes a fast run for a full one.
SLOW_GATES=" suites security-gate contracts:postgres chart:render determinism "

in_profile() {
  case "$PROFILE" in
    all)   return 0 ;;
    quick) [[ "$SLOW_GATES" == *" $1 "* ]] && return 1 ;;
  esac
  return 0
}

run() {
  local name="$1"; shift
  [ "$1" = "--" ] && shift
  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then return 0; fi
  if ! in_profile "$name"; then noprof "$name" "$PROFILE"; return 0; fi
  if "$@"; then ok "$name"; else fail "$name"; fi
}

# Skip a gate whose infra is absent. In --strict this is a FAIL: CI must prove
# it installed the infra, not merely intend to.
wired_skip() {
  local name="$1" reason="$2"
  if [ "$STRICT" = 1 ]; then
    printf '\033[31m  FAIL\033[0m  %s — %s (--strict: a wired gate may not be skipped)\n' "$name" "$reason"
    FAILED+=("$name (skipped under --strict: $reason)")
  else
    skip "$name" "$reason"
  fi
}

# ── service discovery: nothing is hardcoded ─────────────────────────────────
services() {
  local d
  for d in services/*/; do
    d="${d%/}"; d="${d##*/}"
    [ "$d" = "_template" ] && continue
    [ -d "services/$d/app" ] || continue
    printf '%s\n' "$d"
  done
}

# Which interpreter runs a service's tests.
#
# Locally: the service's own venv if it has one (identity needs argon2), else the
# template's shared venv. In CI there are no venvs at all — every service is pip-installed
# into the runner's python — so we must fall back to $PY. Hardcoding the template path here
# would have failed ONLY on the runner, which is exactly the class of bug `--strict` exists
# to prevent. Paths are relative because the caller `cd`s into services/<svc>.
py_for() {
  local svc="$1"
  if [ -x "services/$svc/.venv/Scripts/python.exe" ]; then echo "../$svc/.venv/Scripts/python.exe"
  elif [ -x "services/$svc/.venv/bin/python" ]; then echo "../$svc/.venv/bin/python"
  elif [ -x "services/_template/.venv/Scripts/python.exe" ]; then echo "../_template/.venv/Scripts/python.exe"
  elif [ -x "services/_template/.venv/bin/python" ]; then echo "../_template/.venv/bin/python"
  else echo "$PY"; fi
}

# ── --list ──────────────────────────────────────────────────────────────────
if [ "$PROFILE" = "__list" ]; then
  cat <<'LIST'
GATES (each blocks merge on failure). `--strict` also blocks on a SKIP.

  safety:secrets      FAIL if the diff introduces key/secret material:
                      *.pem *.key id_rsa .secrets .env (not .env.example).
                      A credential must never ride in on an unrelated change.

  safety:ci-parity    META-GATE: every gate this script defines must actually be
                      invoked by .github/workflows/ci.yml, and the workflow may
                      not invoke a gate that does not exist. A gate nobody runs
                      is decoration; a workflow step nobody defines is a lie.

  coverage:services   META-GATE: every discovered service has (a) deploy values,
                      (b) a probe contract test, (c) a hot-path benchmark,
                      (d) the config-knob guard. New services inherit the bar
                      automatically; exceptions live in a SHRINKING allowlist.

  safety:scans        Every scan_all() on a request path is a bug: it costs the
                      size of every tenant's data, and past its cap it returns an
                      ARBITRARY subset, not a truncated one. Each surviving call
                      must carry a `# sweeper:` note saying why reading every row
                      IS the operation. A deliberate sweeper and a forgotten
                      request path look identical from outside; this is what
                      tells them apart.

  safety:product      config/product.json (or the example) is well-formed, ships every
                      customer-visible string in every supported locale, and its
                      FROZEN identifiers still match the committed lock. Changing
                      jwtIssuer / linkNs / signatureDomain rejects every live token,
                      breaks every share link, and invalidates every signature ever
                      made. A rebrand must not be able to do that by accident.

  safety:wiring       Product identity actually reaches a service, and only through
                      knobs that exist. A mapping to JWT_ISSUER when the field is
                      TOKEN_ISSUER deploys clean, is never read, and ships the wrong
                      issuer. Also refuses stale generated values files.

  suites              Every service's pytest suite, under a pinned hash seed.

  security-gate       Every service's ci/security_gate.py: permissive licenses,
                      bandit SAST, secret scan, CVE audit. Under --strict a
                      missing scanner FAILS instead of skipping.

  contracts:postgres  The DataStore contract against a REAL postgres, not just
                      the in-memory adapter. The atomic primitives (incr_if_below,
                      cas) cannot be proven anywhere else: an asyncio.Lock hides
                      every race a database has to serialise itself.
                      Needs DATASTORE_TEST_DSN.

  chart:guards        deploy/chart refuses to render a mistake: scale-out with
                      in-memory drivers, HPA without CPU requests, PDB on a
                      singleton, /internal exposed, an unpinned image, an inert
                      values key. Needs helm.

  chart:render        Every service's values file renders against the chart.
                      Needs helm.

  determinism         `helm template` twice is BYTE-IDENTICAL, and the chart
                      renders with no cluster reachable. Otherwise `helm diff`
                      lies and GitOps flaps forever. Needs helm.
LIST
  exit 0
fi

printf '\033[1mrun_all_gates.sh\033[0m  root=%s\n' "$ROOT"
printf '  python=%s\n  profile=%s  strict=%s  seed=%s\n' \
  "$($PY --version 2>&1)" "$PROFILE" "$STRICT" "$PRIMARY_SEED"
printf '  services=%s\n' "$(services | tr '\n' ' ')"

# =============================================================================
# GATE 1 — safety:secrets   (cheapest, highest blast radius: run it first)
# =============================================================================
want "safety:secrets" && banner "safety:secrets — no credential material in the diff" \
"Scans the branch diff (vs merge-base) for private keys, .secrets and real .env
files. Committing a credential is not a code review comment; it is a rotation."
secrets_guard() {
  local base files hits
  base="$(git merge-base HEAD origin/main 2>/dev/null || git merge-base HEAD main 2>/dev/null || true)"
  if [ -n "$base" ]; then
    files="$(git diff --name-only "$base"...HEAD 2>/dev/null)"
  else
    files="$(git diff --name-only HEAD 2>/dev/null; git diff --name-only --cached 2>/dev/null)"
  fi
  files="$(printf '%s\n' "$files" | sort -u | sed '/^$/d')"
  if [ -z "$files" ]; then echo "  (no changed files in range — nothing to guard)"; return 0; fi
  # .env.example / .env.sample are documentation, not credentials.
  hits="$(printf '%s\n' "$files" \
    | grep -iE '\.pem$|\.key$|(^|/)id_rsa|(^|/)\.secrets|(^|/)\.env$' \
    | grep -vE '\.example$|\.sample$' || true)"
  if [ -n "$hits" ]; then
    echo "  REFUSED — the diff introduces credential material:"
    printf '    %s\n' $hits
    return 1
  fi
  echo "  clean — no credential paths in the diff."
  return 0
}
run "safety:secrets" -- secrets_guard

# =============================================================================
# GATE 2 — safety:ci-parity   (the meta-gate that keeps this file honest)
# The gates and the workflow must agree, in BOTH directions. Without this, a
# gate can be added here and never run in CI (decoration), or a workflow can
# claim a gate that no longer exists (a lie).
# =============================================================================
want "safety:ci-parity" && banner "safety:ci-parity — the workflow runs exactly the gates this script defines" \
"Compares \`run_all_gates.sh --list\` against .github/workflows/ci.yml."
ci_parity() {
  "$PY" tools/ci/check_ci_parity.py
}
run "safety:ci-parity" -- ci_parity

# =============================================================================
# GATE 3 — coverage:services   (META-GATE, modelled on kobo's capability:cover)
# Every shipped service carries the guards this repo learned the hard way.
# Exceptions are explicit, documented, and SHRINKING — never silent.
# =============================================================================
want "coverage:services" && banner "coverage:services — every service carries the guards we learned the hard way" \
"probe contract + hot-path benchmark + config-knob guard + deploy values."
coverage_services() {
  "$PY" tools/ci/check_service_coverage.py
}
run "coverage:services" -- coverage_services

# =============================================================================
# GATE 4 — safety:scans   (no unexplained full-collection read on a request path)
# =============================================================================
want "safety:scans" && banner "safety:scans — no unexplained full-collection read on a request path" "Every surviving scan_all() must say, in a '# sweeper:' note, why reading every row IS the job."
safety_scans() {
  "$PY" tools/ci/check_scan_discipline.py
}
run "safety:scans" -- safety_scans

# =============================================================================
# GATE 5 — safety:product   (identity is config; frozen identifiers are not)
# =============================================================================
want "safety:product" && banner "safety:product — product identity is well-formed and its frozen identifiers have not moved" "--strict: a schema check skipped for a missing jsonschema is a FAILURE, not a pass."
safety_product() {
  # The guard tests run FIRST. The freeze has failed silently twice — once because its baseline
  # never existed, once because `"MIGRATION:" in body` let the commit that documented the rule
  # disarm it. Both times the gate printed a green line. Test the guard, then run it.
  "$PY" -m pytest -q -p no:cacheprovider scripts/tests/test_product_config.py || return 1
  "$PY" scripts/check_product_config.py --strict
}
run "safety:product" -- safety_product

# =============================================================================
# GATE 6 — safety:wiring   (a config nothing reads is a document, not configuration)
# =============================================================================
want "safety:wiring" && banner "safety:wiring — product identity reaches a real Settings field" "Mapped env vars must exist on the service; generated values files must be current."
safety_wiring() {
  "$PY" tools/ci/check_product_wiring.py
}
run "safety:wiring" -- safety_wiring

# =============================================================================
# GATE 7 — suites   (every service's tests, pinned hash seed)
# =============================================================================
want "suites" && banner "suites — every service's pytest suite" \
"Runs under PYTHONHASHSEED=$PRIMARY_SEED so a dict-ordering bug cannot hide."
# Services are INDEPENDENT — that is the point of the architecture — so their suites and their
# scanners have no reason to run one after another. Serially these two gates were the long pole:
# 18 pytest processes, each paying ~1.5s just importing FastAPI, plus 18 pip-audit calls that
# each wait on the network.
#
# Output is collected per service and replayed IN SERVICE ORDER after the join, so a parallel
# run reads exactly like a serial one. A log interleaved by 18 writers is a log nobody reads,
# and a gate nobody reads is a gate nobody trusts.
_jobs() {
  local n
  n="$(nproc 2>/dev/null || echo 4)"
  [ "$n" -gt 2 ] && n=$((n - 2))          # leave the machine usable
  [ "$n" -gt 16 ] && n=16
  [ "$n" -lt 1 ] && n=1
  echo "$n"
}

# fan_out <log-prefix> <fn>: run FN for every service, at most _jobs at a time.
# FN receives (service, python, logfile) and writes its exit code to <logfile>.rc.
fan_out() {
  local prefix="$1" fn="$2" max_override="${3:-}" svc py max running=0
  max="${max_override:-$(_jobs)}"
  rm -f /tmp/"${prefix}"_*.log.rc 2>/dev/null
  for svc in $(services); do
    py="$(py_for "$svc")"
    "$fn" "$svc" "$py" "/tmp/${prefix}_$svc.log" &
    running=$((running + 1))
    if [ "$running" -ge "$max" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
  done
  wait
}

_suite_one() {
  local svc="$1" py="$2" log="$3"
  (cd "services/$svc" && env PYTHONHASHSEED="$PRIMARY_SEED" "$py" -m pytest -q -p no:cacheprovider >"$log" 2>&1)
  echo "$?" > "$log.rc"
}

suites() {
  local svc rc=0 status
  fan_out gate _suite_one
  for svc in $(services); do                 # ordered replay: deterministic, readable
    printf '  --- %s\n' "$svc"
    status="$(cat "/tmp/gate_$svc.log.rc" 2>/dev/null || echo 1)"
    if [ "$status" != "0" ]; then
      echo "      FAILED — tail:"; tail -6 "/tmp/gate_$svc.log" | sed 's/^/        /'; rc=1
    else
      tail -1 "/tmp/gate_$svc.log" | sed 's/^/      /'
    fi
  done
  return $rc
}
run "suites" -- suites

# =============================================================================
# GATE 8 — security-gate   (per service, --strict: no skipped scanner)
# =============================================================================
want "security-gate" && banner "security-gate — licenses / SAST / secrets / CVEs, per service" \
"Under --strict (what CI runs) a scanner that is not installed FAILS the gate.
Locally it runs unstrict: a developer without trivy installed should not be
blocked by a scanner CI is responsible for providing."
_security_one() {
  local svc="$1" py="$2" log="$3" args=()
  [ "$STRICT" = 1 ] && args+=(--strict)
  (cd "services/$svc" && "$py" ci/security_gate.py "${args[@]}" >"$log" 2>&1)
  echo "$?" > "$log.rc"
}

# `pip-audit` is NETWORK-bound, not CPU-bound: it queries the PyPI advisory database. Eighteen at
# once made PyPI return a malformed body to one of them, and the gate went red for a reason that
# had nothing to do with the code. Four at a time is still ~4x faster than serial, and polite.
_SECURITY_JOBS=4

security_gate() {
  local svc rc=0 status
  fan_out sec _security_one "$_SECURITY_JOBS"
  for svc in $(services); do
    status="$(cat "/tmp/sec_$svc.log.rc" 2>/dev/null || echo 1)"
    if [ "$status" = "0" ]; then
      printf '      %-22s PASS\n' "$svc"
    else
      printf '      %-22s FAIL\n' "$svc"; tail -8 "/tmp/sec_$svc.log" | sed 's/^/        /'; rc=1
    fi
  done
  return $rc
}
run "security-gate" -- security_gate

# =============================================================================
# GATE 9 — contracts:postgres
# The atomic primitives cannot be proven on the memory adapter, where an
# asyncio.Lock serialises writers instead of letting them race.
# =============================================================================
want "contracts:postgres" && banner "contracts:postgres — the DataStore contract against a REAL database" \
"incr_if_below (quota) and cas (wallet) are only meaningful under real
concurrency. Set DATASTORE_TEST_DSN; CI provides a postgres service."
contracts_postgres() {
  local svc py rc=0
  for svc in $(services); do
    [ -f "services/$svc/tests/contracts/test_datastore_contract.py" ] || continue
    py="$(py_for "$svc")"
    # `local == CI` is the promise this script makes. py_for prefers a SERVICE'S OWN venv, and
    # nothing checked that venv can run what it is about to be asked to run. identity-service
    # had one without the `postgres` extra: its postgres params errored, the run reported a
    # SKIP, and CI stayed green because CI pip-installs the extras. Say which interpreter is
    # short, and which service, instead of leaving a wall of import errors.
    if ! (cd "services/$svc" && "$py" -c "import asyncpg" >/dev/null 2>&1); then
      printf '      %-22s FAIL  (its interpreter cannot import asyncpg)
' "$svc"
      echo "        $py"
      echo "        install the postgres extra:  $py -m pip install asyncpg"
      rc=1; continue
    fi
    if (cd "services/$svc" && env PYTHONHASHSEED="$PRIMARY_SEED" "$py" -m pytest -q -p no:cacheprovider tests/contracts/ >/tmp/pg_$svc.log 2>&1); then
      printf '      %-22s %s\n' "$svc" "$(tail -1 /tmp/pg_$svc.log)"
    else
      printf '      %-22s FAIL\n' "$svc"; tail -6 "/tmp/pg_$svc.log" | sed 's/^/        /'; rc=1
    fi
  done
  # Prove the postgres params were actually exercised, not silently skipped.
  if grep -qi "skipped" /tmp/pg_*.log 2>/dev/null; then
    echo "  a postgres contract param was SKIPPED — DATASTORE_TEST_DSN is not reaching the tests"
    return 1
  fi
  return $rc
}
if [ -n "${DATASTORE_TEST_DSN:-}" ]; then
  run "contracts:postgres" -- contracts_postgres
elif want "contracts:postgres"; then
  if in_profile "contracts:postgres"; then
    wired_skip "contracts:postgres" "DATASTORE_TEST_DSN unset (no database)"
  else
    noprof "contracts:postgres" "$PROFILE"
  fi
fi

# =============================================================================
# GATE 10/11/12 — the chart. Needs helm.
# =============================================================================
chart_guards()  { env PYTHONHASHSEED="$PRIMARY_SEED" "$PY" -m pytest tests/deploy -q -p no:cacheprovider; }

chart_render() {
  local f n rc=0
  for f in deploy/services/*.values.yaml; do
    n="$(basename "$f" .values.yaml)"
    if ! "$HELM" template "$n" deploy/chart -f "$f" >/dev/null 2>&1; then
      echo "      $n FAILED to render"; rc=1
    fi
  done
  [ $rc -eq 0 ] && echo "      all $(ls deploy/services/*.values.yaml | wc -l) services render"
  return $rc
}

determinism() {
  # `helm template` must be pure: same input -> byte-identical output, offline.
  # This is the chart's analogue of kobo's second-hash-seed corpus re-run.
  local f n a b rc=0
  for f in deploy/services/*.values.yaml; do
    n="$(basename "$f" .values.yaml)"
    a="$("$HELM" template "$n" deploy/chart -f "$f" 2>/dev/null)"
    b="$(KUBECONFIG=/nonexistent "$HELM" template "$n" deploy/chart -f "$f" 2>/dev/null)"
    if [ -z "$a" ] || [ "$a" != "$b" ]; then
      echo "      $n: render is not deterministic/offline (a \`lookup\` crept in?)"; rc=1
    fi
  done
  [ $rc -eq 0 ] && echo "      every chart render is byte-identical and needs no cluster"
  return $rc
}

if have "$HELM"; then
  want "chart:guards" && banner "chart:guards — the chart refuses to render a mistake" \
"scale-out on memory drivers, HPA without cpu requests, PDB on a singleton,
/internal exposed, an unpinned image, an inert values key."
  run "chart:guards" -- chart_guards

  want "chart:render" && banner "chart:render — every service's values renders" ""
  run "chart:render" -- chart_render

  want "determinism" && banner "determinism — helm template is pure" \
"Two renders must be byte-identical, and must succeed with no cluster reachable.
Otherwise \`helm diff\` lies and GitOps flaps forever."
  run "determinism" -- determinism
else
  for g in chart:guards chart:render determinism; do
    want "$g" || continue
    in_profile "$g" && wired_skip "$g" "helm not installed" || noprof "$g" "$PROFILE"
  done
fi

# =============================================================================
# SUMMARY
# =============================================================================
printf '\n\033[1m================= GATE SUMMARY =================\033[0m\n'
printf '  passed: %s\n' "${#PASSED[@]}"
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  printf '\033[33m  SKIPPED (wired, not run here):\033[0m\n'
  printf '    - %s\n' "${SKIPPED[@]}"
  printf '    (CI runs --strict, where a skipped gate FAILS)\n'
fi
if [ "${#FAILED[@]}" -gt 0 ]; then
  printf '\033[31m  FAILED gates:\033[0m\n'
  printf '    - %s\n' "${FAILED[@]}"
  printf '\033[31m\nRESULT: GATES FAILED — merge blocked.\033[0m\n'
  exit 1
fi
printf '\033[32m\nRESULT: ALL RUNNABLE GATES PASSED.\033[0m\n'
exit 0
