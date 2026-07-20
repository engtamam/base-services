#!/usr/bin/env bash
# =============================================================================
# gatelib.sh — everything a gate file may rely on.
#
# Sourced by run_all_gates.sh, and then by every gate in $GATES_DIR. A gate file
# declares metadata and a `gate_main`; it never reimplements discovery, fan-out,
# or interpreter selection.
#
# FOUR OUTCOMES, because three conflated two different facts:
#
#   PASS      the gate ran and held
#   FAIL      the gate ran and caught something          -> merge blocked
#   SKIP      the gate is wired, its INFRA is absent     -> --strict makes it FAIL
#   PENDING   the gate is wired, its SUBJECT is not yet  -> loud, never blocks
#             built
#
# A gate wired before the code it guards exists is a PROMISE. A gate whose tool is
# missing is a HOLE. Failing CI for the first teaches people that --strict is
# noise, and then a real SKIP gets waved through. Our `container (trivy)` gate
# skipped silently for months behind a green board for exactly that reason.
# =============================================================================

# ── unit discovery: WHERE to look and WHAT marks a unit are config ───────────
#   GATE_UNITS_DIR    directory holding the units        (default: services)
#   GATE_UNIT_MARKER  subdirectory that marks a unit     (default: app)
#   GATE_UNIT_SKIP    space-separated units to ignore    (default: _template)
UNITS_DIR="${GATE_UNITS_DIR:-services}"
UNIT_MARKER="${GATE_UNIT_MARKER:-app}"
UNIT_SKIP="${GATE_UNIT_SKIP:-_template}"

# GATE_ONLY_UNIT restricts discovery to ONE unit, so a CI matrix can fan the per-service gates out
# across 17 runners instead of grinding through them one after another in a single job. It is a
# FILTER, never a skip: every unit still runs its gates, in some job, and `require_units` still
# treats an empty result as fatal — so a typo'd unit name is a hard error, not a quiet green.
# GATE_CHANGED_ONLY is a DEV-LOOP accelerator, OPT-IN and OFF by default. When =1, discovery is
# narrowed to units with a git diff vs $GATE_CHANGED_BASE (default origin/main). The release/CI
# run leaves it unset and gates every unit. Two safety rails keep "faster" from ever meaning
# "fewer checks": if the base ref is unavailable OR the narrowed set is empty, it FALLS BACK to
# full discovery — so it can only ever be a SUBSET when git positively proves the rest is
# untouched, and require_units still fatals on a genuine 0-unit misconfiguration.
_changed_units() {
  local base="${GATE_CHANGED_BASE:-origin/main}" diffbase line u seen=""
  command -v git >/dev/null 2>&1 || return 1
  diffbase="$(git merge-base "$base" HEAD 2>/dev/null)" || diffbase="$base"
  git rev-parse --verify "$diffbase" >/dev/null 2>&1 || return 1
  git diff --name-only "$diffbase" -- "$UNITS_DIR/" 2>/dev/null | while IFS= read -r line; do
    u="${line#"$UNITS_DIR"/}"; u="${u%%/*}"
    [ -n "$u" ] && printf '%s\n' "$u"
  done | sort -u
}

services() {
  local d skip hit changed=""
  if [ -n "${GATE_ONLY_UNIT:-}" ]; then
    if [ -d "$UNITS_DIR/$GATE_ONLY_UNIT/$UNIT_MARKER" ]; then
      printf '%s
' "$GATE_ONLY_UNIT"
    fi
    return 0
  fi
  if [ "${GATE_CHANGED_ONLY:-0}" = "1" ]; then
    changed="$(_changed_units)"          # may be empty -> we fall back to full discovery below
  fi
  for d in "$UNITS_DIR"/*/; do
    [ -d "$d" ] || continue
    d="${d%/}"; d="${d##*/}"
    hit=0
    for skip in $UNIT_SKIP; do [ "$d" = "$skip" ] && hit=1; done
    [ "$hit" = "1" ] && continue
    [ -d "$UNITS_DIR/$d/$UNIT_MARKER" ] || continue
    if [ -n "$changed" ] && ! printf '%s\n' "$changed" | grep -qx "$d"; then continue; fi
    printf '%s\n' "$d"
  done
}

# A gate that iterates an EMPTY set passes. Loudly, in green, having done nothing.
#
# Pointed at a repo with no `services/`, this runner printed "ALL RUNNABLE GATES PASSED" while
# running zero tests and zero scanners. Discovery finding nothing is a CONFIGURATION ERROR,
# never a clean run.
require_units() {
  local n
  n="$(services | grep -c . || true)"
  if [ "${n:-0}" -eq 0 ]; then
    echo "  FATAL: discovered 0 units under '$UNITS_DIR/' (marker: '$UNIT_MARKER/')." >&2
    echo "         Every per-unit gate would iterate an empty set and report PASS." >&2
    echo "         Set GATE_UNITS_DIR / GATE_UNIT_MARKER for this repo's layout." >&2
    return 1
  fi
  return 0
}

# ── interpreter selection ───────────────────────────────────────────────────
# A unit's own venv if it has one (identity needs argon2), else the template's, else $PY.
# `local == CI` is the promise this runner makes, so a gate that needs an extra must CHECK the
# interpreter can import it rather than let pytest emit a wall of import errors.
py_for() {
  local svc="$1"
  if   [ -x "$UNITS_DIR/$svc/.venv/Scripts/python.exe" ]; then echo "../$svc/.venv/Scripts/python.exe"
  elif [ -x "$UNITS_DIR/$svc/.venv/bin/python" ];         then echo "../$svc/.venv/bin/python"
  elif [ -x "$UNITS_DIR/_template/.venv/Scripts/python.exe" ]; then echo "../_template/.venv/Scripts/python.exe"
  elif [ -x "$UNITS_DIR/_template/.venv/bin/python" ];    then echo "../_template/.venv/bin/python"
  else echo "$PY"; fi
}

have() { command -v "$1" >/dev/null 2>&1; }

# ── parallelism ─────────────────────────────────────────────────────────────
# A gate that fans out must declare WHAT IT SHARES. Four times in one session a shared resource
# under concurrency produced a failure that accused the code:
#
#   one postgres + N pytest processes  -> concurrent TRUNCATE, read as adapter divergence
#   one PyPI advisory DB + 18 pip-audit -> a malformed body, read as a CVE
#   one /tmp/gate_<svc>.log + two runs -> overwritten logs, read as 14 failing services
#
# So: cpu-bound gates get all cores; network-bound gates pass a lower cap; gates that own a
# database stay serial.
_jobs() {
  local n
  n="$(nproc 2>/dev/null || echo 4)"
  [ "$n" -gt 2 ] && n=$((n - 2))          # leave the machine usable
  [ "$n" -gt 16 ] && n=16
  [ "$n" -lt 1 ] && n=1
  echo "$n"
}

# fan_out <log-prefix> <fn> [max-jobs]
# FN receives (unit, python, logfile) and writes its exit code to <logfile>.rc.
# Output is replayed IN UNIT ORDER by the caller: a log interleaved by 18 writers is a log
# nobody reads, and a gate nobody reads is a gate nobody trusts.
fan_out() {
  local prefix="$1" fn="$2" max_override="${3:-}" svc py max running=0
  max="${max_override:-$(_jobs)}"
  rm -f "$RUN_TMP/${prefix}"_*.log.rc 2>/dev/null
  for svc in $(services); do
    py="$(py_for "$svc")"
    "$fn" "$svc" "$py" "$RUN_TMP/${prefix}_$svc.log" &
    running=$((running + 1))
    if [ "$running" -ge "$max" ]; then wait -n 2>/dev/null || wait; running=$((running - 1)); fi
  done
  wait
}

# ── outcome helpers, callable from inside gate_main ──────────────────────────
# `pending` is the one that needs saying out loud: it means the SUBJECT does not exist yet.
# Return 0 from gate_main after calling it.
GATE_OUTCOME=""
pending() { GATE_OUTCOME="PENDING"; GATE_REASON="${1:-subject not built yet}"; }
skip()    { GATE_OUTCOME="SKIP";    GATE_REASON="${1:-infra absent}"; }
