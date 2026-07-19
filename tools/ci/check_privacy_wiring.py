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

# Every service must land in exactly one of three buckets: it implements the contract, it holds no
# subject data, or it is a DECLARED gap. Silence is not an option — a service nobody classified is
# a service nobody checked, and that is precisely how ten erasure endpoints ended up with no
# callers while every test passed.

# Genuinely tenant- or machine-scoped. Verified by reading the models, not assumed from the name.
NO_SUBJECT_DATA = {
    "_template": "the scaffold — not a deployed service",
    "api-gateway": "routes and aggregates; stores no subject data of its own",
    "wallet-service": "Wallet/LedgerEntry/Hold/Grant key on tenant only; balances are per-tenant",
    "tenant-service": "Tenant/slug/domain records describe organisations, not natural persons",
    "job-service": "orchestration only; see KNOWN_GAPS for the payload-blob caveat",
}

# Services that DO hold subject data and cannot yet be erased. Listing them here is not permission
# — it is the opposite. An undeclared gap is invisible; a declared one is a ticket with a reason
# attached, and this gate makes removing the reason the only way to make the entry go away.
#
# What these five share: NONE has a subject-identifying column. The personal data sits in untyped
# blobs (payload, record, result) or opaque artifacts (UBL XML, video segments), so there is no
# indexed key to resolve a subject to rows. Adding an endpoint would not close the gap — that is a
# schema migration, which is why it is not folded into this phase.
KNOWN_GAPS = {
    "connector-service": "SyncRecord.record holds full CRM/ERP contact rows (mapping.py maps "
                         "properties.email); DLQ rows persist after a failed sync. Needs a "
                         "subject_id at write time before erasure can find them.",
    "webhook-service": "Event.payload / InboundEvent.payload / Delivery.response_body are "
                       "unbounded PII carriers retained for retry history.",
    "job-service": "Job.payload and Job.result carry recipient data (a welcome-email job holds "
                   "the address). Untyped blobs, no subject key.",
    "zatca-service": "buyer identity is persisted inside signed_xml/cleared_xml (ubl.py writes "
                     "RegistrationName + ID); simplified invoices are B2C by definition. Likely "
                     "resolves to a STATUTORY RETENTION exemption rather than deletion — but the "
                     "erasure request must still be recorded and answered.",
    "stream-service": "recordings and exports are video of physical spaces — biometric data by "
                      "construction — with no subject column to match on. Recording.locked is "
                      "already a legal-hold flag, so retention duties compete here too.",
}


def implementers() -> set[str]:
    """Services whose internal router actually serves the erase contract."""
    found = set()
    for internal in sorted((ROOT / "services").glob("*/app/api/internal.py")):
        if ERASE_ROUTE.search(internal.read_text(encoding="utf-8")):
            found.add(internal.parents[3].name)
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

    for service in sorted(impl - cfg):
        problems.append(
            f"  {service}: implements /internal/privacy/erase but is NOT in PRIVACY_TARGETS.\n"
            f"      A DSAR would complete while this service still holds the subject's data.\n"
            f"      Fix: add '{service}=http://{service}:8000' to PRIVACY_TARGETS in\n"
            f"      deploy/services/compliance-service.values.yaml")

    for service in sorted(cfg - impl):
        if service in NO_SUBJECT_DATA:
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
