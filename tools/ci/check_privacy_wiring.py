#!/usr/bin/env python3
"""safety:privacy-wiring — the erasure fan-out must reach every service that holds subject data.

Two failure modes, both of which have already happened once in this repo and neither of which any
test caught, because each half is individually correct:

  R1  A service implements /internal/privacy/erase but is NOT in PRIVACY_TARGETS.
      The endpoint works. The data map may even list it. Nothing ever calls it, so a DSAR completes
      with that service's copy of the subject untouched — the most dangerous shape of bug here,
      because the system reports success.

  R2  PRIVACY_TARGETS names a service that does NOT implement the endpoint.
      Every erasure to it fails, every DSAR containing it hangs to its SLA deadline. Loud rather
      than silent, but still a broken promise.

This is the "built but unwired" class the phase set out to make impossible. Ten erasure endpoints
sat in this codebase with zero callers; the code was right, the wiring was absent, and every test
passed. A gate is the only thing that can see across that seam, because it is the only check that
looks at the implementations and the deployed config at the same time.

Exit 1 on any violation, naming the service and which side of the seam is missing.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
VALUES = ROOT / "deploy" / "services" / "compliance-service.values.yaml"
ERASE_ROUTE = re.compile(r'@router\.post\(\s*["\']/privacy/erase["\']')

# Every service must land in exactly one of two buckets: it implements the erasure contract, or it
# is the scaffold. There is no third bucket any more.
#
# There WAS one. An audit during Phase 37 found five services holding real subject data with no
# erase endpoint — connector (CRM rows in the DLQ), webhook (payload blobs), job (job arguments),
# zatca (buyer identity inside signed XML), stream (video of people). They were listed here as
# declared gaps with reasons. Every one has since been closed, and the list is gone rather than
# kept as a place for the next gap to be parked.
#
# The lesson that stays: NONE of those five had a subject-identifying column. The personal data sat
# in untyped blobs and opaque artifacts, which is why the audit was needed at all — no schema
# search would have found it. If a new service is added here as an exemption, verify it by reading
# the models, not by trusting the name.
EXEMPT = {
    "_template": "the scaffold — not a deployed service; it ships the contract for new services",
}


def implementers() -> set[str]:
    """Services whose internal router actually serves the erase contract."""
    found = set()
    for internal in sorted((ROOT / "services").glob("*/app/api/internal.py")):
        if ERASE_ROUTE.search(internal.read_text(encoding="utf-8")):
            # services/<name>/app/api/internal.py -> parents[2] is <name>
            found.add(internal.parents[2].name)
    return found


def configured() -> set[str]:
    """Target names in the deployed PRIVACY_TARGETS, parsed from the values file.

    Read as text rather than via a YAML lib so this gate has no dependency beyond the stdlib —
    a gate that cannot run is a gate that does not protect anything.
    """
    text = VALUES.read_text(encoding="utf-8")
    match = re.search(r"^\s*PRIVACY_TARGETS:\s*>-?\s*\n((?:\s{4,}.*\n)+)", text, re.MULTILINE)
    if not match:
        return set()
    return {entry.split("=", 1)[0].strip()
            for entry in match.group(1).replace("\n", " ").split(",") if entry.strip()}


def main() -> int:
    impl, cfg = implementers(), configured()
    problems = []

    for service in sorted(impl - cfg - set(EXEMPT)):
        problems.append(
            f"  {service}: implements /internal/privacy/erase but is NOT in PRIVACY_TARGETS.\n"
            f"      A DSAR would complete while this service still holds the subject's data.\n"
            f"      Fix: add '{service}=http://{service}:8000' to PRIVACY_TARGETS in\n"
            f"      deploy/services/compliance-service.values.yaml")

    for service in sorted(cfg - impl):
        if service in EXEMPT:
            continue
        problems.append(
            f"  {service}: named in PRIVACY_TARGETS but does NOT implement /internal/privacy/erase.\n"
            f"      Every erasure to it fails and every DSAR including it hangs to its deadline.\n"
            f"      Fix: implement the contract (docs/contracts/privacy-erase.schema.json) or\n"
            f"      remove it from PRIVACY_TARGETS.")

    if problems:
        print("      erasure fan-out is not wired to reality:")
        print("\n".join(problems))
        return 1

    print(f"      {len(impl)} services implement the erase contract; all are dispatch targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
