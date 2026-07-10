#!/usr/bin/env python
"""META-GATE: every shipped service carries the guards this repo learned the hard way.

Modelled on kobo's `capability:cover`. The idea that makes it work is not the check —
it is that the exceptions live in a **documented, shrinking allowlist**, so a gap is a
visible debt with a name on it rather than an absence nobody notices.

Each requirement below exists because its absence shipped a real defect:

  probe contract    liveness pointed at a dependency would restart the whole fleet on
                    one slow database moment.
  hot-path guard    the authz PDP scanned every policy per decision; billing scanned the
                    subscriptions table per entitlement check. Both looked fine.
  knob guard        feature-flag, webhook, search and connector each shipped a config knob
                    that nothing read. The value was documented, configurable, and inert.
  deploy values     a service that cannot be deployed is not finished; and without its
                    values file the chart guards never see it.

A new service inherits the bar automatically: it is discovered, not listed.
"""
from __future__ import annotations

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
DEPLOY_VALUES = ROOT / "deploy" / "services"


def _has_test_named(service: pathlib.Path, *needles: str) -> bool:
    """True if any test file contains one of `needles` as a test function name."""
    tests = service / "tests"
    if not tests.is_dir():
        return False
    for path in tests.rglob("test_*.py"):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if any(f"def {n}" in text for n in needles):
            return True
    return False


def _contract_runs_against_postgres(service: pathlib.Path) -> bool:
    """The DataStore contract must ALSO run against a real postgres, not just memory.

    Sixteen services shipped a contract file whose only parameter was `MemoryDataStore`. They
    passed, quickly, and proved that the in-memory adapter agrees with itself — while its
    `asyncio.Lock` serialises every writer a real database has to let race.

    `contracts:postgres` could not see it. That gate fails when a postgres param is SKIPPED, and
    a param that was never defined is never skipped. An absent test does not fail; it is simply
    absent, and a green board looks the same either way.
    """
    contract = service / "tests" / "contracts" / "test_datastore_contract.py"
    if not contract.is_file():
        return False
    try:
        text = contract.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return "DATASTORE_TEST_DSN" in text and "PostgresDataStore" in text


REQUIREMENTS = {
    "probe-contract": lambda s: (s / "tests/integration/test_probe_contract.py").exists(),
    "hot-path-guard": lambda s: _has_test_named(
        s, "test_healthz_touches_no_dependency") or any(
        (s / "tests/unit").glob("test_*hot_path*.py")),
    "knob-guard": lambda s: _has_test_named(s, "test_every_settings_field_is_consumed_somewhere"),
    "deploy-values": lambda s: (DEPLOY_VALUES / f"{s.name}.values.yaml").exists(),
    "postgres-contract": lambda s: _contract_runs_against_postgres(s),
}

# ── The SHRINKING known-gaps allowlist ──────────────────────────────────────
# Format: {service: {requirement: "why, and what closes it"}}
# Adding an entry is a deliberate, reviewed admission of debt. Removing the last
# one deletes this block. An entry that is no longer needed FAILS the gate, so it
# can never quietly become permanent.
KNOWN_GAPS: dict[str, dict[str, str]] = {
    # e.g. "some-service": {"hot-path-guard": "no hot path; issue #123"}
}


def services() -> list[pathlib.Path]:
    return sorted(p for p in SERVICES.iterdir()
                  if p.is_dir() and p.name != "_template" and (p / "app").is_dir())


def main() -> int:
    missing: list[str] = []
    stale: list[str] = []

    for svc in services():
        gaps = KNOWN_GAPS.get(svc.name, {})
        for req, check in REQUIREMENTS.items():
            present = check(svc)
            allowed = req in gaps
            if present and allowed:
                stale.append(f"{svc.name}:{req} — allowlisted but PRESENT: delete the entry")
            elif not present and not allowed:
                missing.append(f"{svc.name}:{req}")

    # An allowlist entry for a service that no longer exists is also stale.
    known = {p.name for p in services()}
    for name in KNOWN_GAPS:
        if name not in known:
            stale.append(f"{name} — allowlisted service does not exist")

    if missing:
        print("FAIL: services missing a guard this repo requires:")
        for m in sorted(missing):
            print(f"    - {m}")
        print("  Add the guard, or add a documented entry to KNOWN_GAPS with a reason.")
    if stale:
        print("FAIL: the known-gaps allowlist is stale (it must SHRINK, never rot):")
        for s in sorted(stale):
            print(f"    - {s}")

    if missing or stale:
        return 1

    n = len(services())
    print(f"  all {n} services carry: {', '.join(REQUIREMENTS)}")
    if KNOWN_GAPS:
        print(f"  known gaps outstanding: {sum(len(v) for v in KNOWN_GAPS.values())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
