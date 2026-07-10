#!/usr/bin/env bash
# =============================================================================
# run_all_gates.sh — the SINGLE SOURCE OF TRUTH for the merge-blocking gates.
#
# CI calls this. You call this. `local == CI` is the whole point: a gate that only
# exists inside a YAML file is a gate nobody can run before pushing.
#
# NOTHING IS HARDCODED HERE. Gates are DISCOVERED from $GATE_GATES_DIR, one file
# per gate; units are DISCOVERED from $GATE_UNITS_DIR. Point this script at
# another repository and it gates that repository. Nobody edits this file to add
# a gate — that was the one thing we promised downstream consumers.
#
# FOUR OUTCOMES:
#     PASS      the gate ran and held
#     FAIL      the gate ran and caught something        -> merge blocked
#     SKIP      the gate is wired, its INFRA is absent   -> --strict makes it FAIL
#     PENDING   the gate is wired, its SUBJECT is not
#               built yet                                -> loud, never blocks
#
# A gate that silently passes because its tool was missing is worse than no gate.
# A gate that fails because the code it guards is not written yet teaches people
# to ignore --strict, and then a real SKIP gets waved through. Different facts.
#
# Usage:
#   tools/ci/run_all_gates.sh                 # everything runnable here
#   tools/ci/run_all_gates.sh --strict        # a SKIP is a FAIL (what CI runs)
#   tools/ci/run_all_gates.sh --profile quick # fast pre-commit lane
#   tools/ci/run_all_gates.sh --list          # every gate + what it protects
#   tools/ci/run_all_gates.sh --only chart:guards
#   tools/ci/run_all_gates.sh --verify        # prove each gate can FAIL
#
# Env:
#   GATE_GATES_DIR       where the gate files live   (default: tools/ci/gates.d)
#   GATE_UNITS_DIR       where the units live        (default: services)
#   GATE_UNIT_MARKER     what marks a unit           (default: app)
#   GATE_UNIT_SKIP       units to ignore             (default: _template)
#   DATASTORE_TEST_DSN   run the DataStore contract against a REAL postgres
#   HELM                 path to helm (default: on PATH)
#   GATE_SEED            primary PYTHONHASHSEED (default 0)
#   GATE_SEED2           second seed for the determinism gate (default 1337)
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

# Per-RUN scratch. Two invocations used to write the same /tmp/gate_<svc>.log, so a second run
# silently overwrote the first one's results and both reported nonsense. Shared mutable state
# that nobody declared — the same bug as pointing two test processes at one database.
RUN_TMP="$(mktemp -d 2>/dev/null || echo "/tmp/gates.$$")"
mkdir -p "$RUN_TMP"
trap 'rm -rf "$RUN_TMP"' EXIT

PY_DEFAULT="services/_template/.venv/Scripts/python.exe"
[ -x "$PY_DEFAULT" ] || PY_DEFAULT="services/_template/.venv/bin/python"
[ -x "$PY_DEFAULT" ] || PY_DEFAULT="python"
PY="${PYTHON:-$PY_DEFAULT}"
HELM="${HELM:-helm}"
PRIMARY_SEED="${GATE_SEED:-0}"
SECOND_SEED="${GATE_SEED2:-1337}"
export PY HELM PRIMARY_SEED SECOND_SEED RUN_TMP ROOT

GATES_DIR="${GATE_GATES_DIR:-tools/ci/gates.d}"

STRICT=0
MODE="run"
PROFILE="${GATE_PROFILE:-all}"
ONLY=""
export STRICT

FAILED=(); SKIPPED=(); PASSED=(); PENDINGS=()

# shellcheck source=tools/ci/gatelib.sh
. "$ROOT/tools/ci/gatelib.sh"

while [ $# -gt 0 ]; do
  case "$1" in
    --strict)  STRICT=1; export STRICT; shift ;;
    --profile) PROFILE="$2"; shift 2 ;;
    --only)    ONLY="$2"; shift 2 ;;
    --list)    MODE="list"; shift ;;
    --verify)  MODE="verify"; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

# ── gate discovery ──────────────────────────────────────────────────────────
gate_files() {
  local f
  for f in "$GATES_DIR"/*.gate; do
    [ -f "$f" ] || continue
    printf '%s\n' "$f"
  done
}

# A runner with no gates passes everything. Same defect as a gate with no units: a loop over an
# empty set exits 0 and prints green.
require_gates() {
  local n
  n="$(gate_files | grep -c . || true)"
  if [ "${n:-0}" -eq 0 ]; then
    echo "FATAL: no gate files in '$GATES_DIR/'. Nothing would be checked and this script" >&2
    echo "       would exit 0. Set GATE_GATES_DIR to this repository's gates." >&2
    exit 1
  fi
}
require_gates

# Read one metadata field WITHOUT executing the file. `--list` must be safe to run against a
# repository whose gates you have not read.
#
# Quotes are OPTIONAL. The first version of this required them, so `gate_units=1` read as the
# empty string, every per-unit gate skipped its require_units check, and a run against a repo
# with no units passed green — the exact bug this phase exists to remove, reintroduced by the
# parser that was supposed to remove it. A metadata reader that silently returns "" for a value
# it cannot parse is a metadata reader that lies.
meta() {
  sed -n "s/^$2=\"\(.*\)\"\$/\1/p;s/^$2=\([^\"].*\)\$/\1/p" "$1" | head -1
}

protects() {
  sed -n '/^gate_protects="/,/"$/p' "$1" | sed '1s/^gate_protects="//' | sed '$s/"$//'
}

# ── reporting ───────────────────────────────────────────────────────────────
banner()   { printf '\n\033[1m=== %s ===\033[0m\n%s\n' "$1" "$2"; }
pass_msg() { printf '\033[32m  PASS\033[0m  %s\n' "$1"; PASSED+=("$1"); }
fail_msg() { printf '\033[31m  FAIL\033[0m  %s\n' "$1"; FAILED+=("$1"); }
skip_msg() { printf '\033[33m  SKIP (WIRED)\033[0m  %s — %s\n' "$1" "$2"; SKIPPED+=("$1 — $2"); }
pend_msg() { printf '\033[36m  PENDING\033[0m  %s — %s\n' "$1" "$2"; PENDINGS+=("$1 — $2"); }
noprof()   { printf '\033[36m  N/A\033[0m   %s — not in the %s profile\n' "$1" "$2"; }

in_profile() {
  case "$PROFILE" in
    all)   return 0 ;;
    quick) [ "$1" = "1" ] && return 1 ;;
  esac
  return 0
}

# ── --list: GENERATED from the gate files ───────────────────────────────────
# The memo asked parity to check three places: described, implemented, run. We check two and
# DELETE the third. A gate's description lives beside its command, so "implemented but never
# described" is unrepresentable. Do not gate a class of bug you can make impossible.
if [ "$MODE" = "list" ]; then
  echo "GATES (each blocks merge on failure). \`--strict\` also blocks on a SKIP."
  echo
  for f in $(gate_files); do
    printf '  %-20s %s\n' "$(meta "$f" gate_name)" "$(meta "$f" gate_desc)"
    protects "$f" | sed 's/^/                       /'
    echo
  done
  echo "Outcomes: PASS | FAIL | SKIP (infra absent) | PENDING (subject not built yet)"
  exit 0
fi

# ── --verify: a gate that cannot fail is decoration ─────────────────────────
# gates.d makes gates cheap to add, which makes DECORATION cheap to add. A gate file may declare
# `gate_selftest`: a command that must FAIL on a known-bad input. This reports which gates have
# proven they can fail, and which have only ever been seen to pass.
if [ "$MODE" = "verify" ]; then
  rc=0; proven=0; unproven=()
  for f in $(gate_files); do
    name="$(meta "$f" gate_name)"
    if grep -q '^gate_selftest()' "$f"; then
      if ( set +e; . "$ROOT/tools/ci/gatelib.sh"; . "$f"; gate_selftest >/dev/null 2>&1 ); then
        printf '\033[32m  PROVEN\033[0m   %s — fails on a known-bad input\n' "$name"
        proven=$((proven + 1))
      else
        printf '\033[31m  BROKEN\033[0m   %s — its selftest did not fail; the gate may be inert\n' "$name"
        rc=1
      fi
    else
      unproven+=("$name")
    fi
  done
  echo
  printf '  %s gate(s) proven able to fail.\n' "$proven"
  if [ "${#unproven[@]}" -gt 0 ]; then
    printf '\033[33m  no selftest (never seen to fail):\033[0m\n'
    printf '    - %s\n' "${unproven[@]}"
  fi
  exit $rc
fi

# ── run ─────────────────────────────────────────────────────────────────────
for f in $(gate_files); do
  name="$(meta "$f" gate_name)"
  desc="$(meta "$f" gate_desc)"
  needs="$(meta "$f" gate_needs)"
  need_env="$(meta "$f" gate_env)"
  subject="$(meta "$f" gate_subject)"
  units="$(meta "$f" gate_units)"
  slow="$(meta "$f" gate_slow)"

  if [ -n "$ONLY" ] && [ "$ONLY" != "$name" ]; then continue; fi
  if ! in_profile "${slow:-0}"; then noprof "$name" "$PROFILE"; continue; fi

  # PENDING: the SUBJECT does not exist yet. Never blocks. A gate wired the day before the code
  # it guards is a promise kept, not a build broken.
  if [ -n "$subject" ] && ! compgen -G "$subject" >/dev/null 2>&1; then
    pend_msg "$name" "subject not built yet: $subject"
    continue
  fi

  # SKIP: the INFRA is absent. --strict turns this into a FAIL, because "we installed helm and
  # postgres" must be a proven fact, not a YAML comment.
  missing=""
  for c in $needs;    do have "$c"        || missing="$missing $c"; done
  for e in $need_env; do [ -n "${!e:-}" ] || missing="$missing \$$e"; done
  if [ -n "$missing" ]; then
    if [ "$STRICT" = 1 ]; then
      fail_msg "$name"; echo "        --strict: required and absent:$missing"
    else
      skip_msg "$name" "absent:$missing"
    fi
    continue
  fi

  banner "$name — $desc" "$(protects "$f")"

  # Each gate runs in a SUBSHELL: it cannot corrupt the runner, or the gate after it.
  if [ "${units:-0}" = "1" ]; then
    ( . "$ROOT/tools/ci/gatelib.sh"; . "$f"; require_units && gate_main )
  else
    ( . "$ROOT/tools/ci/gatelib.sh"; . "$f"; gate_main )
  fi
  if [ $? -eq 0 ]; then pass_msg "$name"; else fail_msg "$name"; fi
done

# ── summary ─────────────────────────────────────────────────────────────────
printf '\n\033[1m================= GATE SUMMARY =================\033[0m\n'
printf '  passed: %s\n' "${#PASSED[@]}"
if [ "${#PENDINGS[@]}" -gt 0 ]; then
  printf '\033[36m  PENDING (wired; the code they guard is not written yet):\033[0m\n'
  printf '    - %s\n' "${PENDINGS[@]}"
  printf '    (these never block: a promise kept, not a build broken)\n'
fi
if [ "${#SKIPPED[@]}" -gt 0 ]; then
  printf '\033[33m  SKIPPED (wired, infra absent):\033[0m\n'
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
