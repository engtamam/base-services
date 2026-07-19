#!/usr/bin/env python3
"""safety:dead-code — a symbol nobody calls is a rule that can rot.

Prompted by a real bug. `Answer.is_satisfied` carried the reasoning for the single most
safety-critical decision in onboarding — "unknown must never count as satisfied" — in a property
that nothing called. The evaluator duplicated the rule as a bare string comparison. Mutating the
careful version changed no behaviour and broke no test, because it was decoration. The dangerous
half was the DUPLICATION; the visible half was the dead symbol, and this gate catches the visible
half so the dangerous half has nowhere to hide.

**What it reports, and what it deliberately ignores.**

Vulture at high confidence is dominated by one false-positive class: parameters an interface
REQUIRES but a particular implementation does not read. `labels` on a telemetry port, `tb` on a
context manager's __exit__, `full_path` on a FastAPI catch-all route — every one is load-bearing
for the signature and unused in the body. Reporting those would produce twenty findings per
service that are all correct code, and a gate that cries wolf every run is a gate people learn to
skip past.

So this reports the kinds that indicate genuinely orphaned code:

    unused import      almost always real, and free to fix
    unused function    nobody calls it
    unused class       nobody constructs it
    unused method      nobody dispatches to it
    unused property    exactly the is_satisfied case
    unused attribute   set and never read

and ignores `unused variable`, which is where the interface-parameter noise lives.

**The honest limit.** This finds a symbol nobody uses. It cannot find the more dangerous shape —
the same rule written twice, where one copy is live and correct and the other silently rots. Only
mutation testing found that. Both are needed; neither replaces the other.

Exceptions live in an allowlist with a REASON, so a suppression is a decision someone wrote down.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# The kinds worth failing on. `unused variable` is excluded on purpose — see the header.
REPORTED = ("unused import", "unused function", "unused class", "unused method",
            "unused property")

# `unused attribute` is excluded alongside `unused variable`, and for a concrete reason: a dataclass
# field that leaves the service through asdict()/to_dict() has no textual reader, so vulture calls
# `updated_at`, `next_retry_at` and friends dead while they are serialised on every response.

# WHERE a symbol-level finding is trustworthy: app/core, the domain logic. Nothing there is
# dispatched by a framework, so an unused symbol really is unused.
#
# app/ports is excluded for a reason specific to what a port IS. A Protocol method has no caller by
# construction — adapters implement it and the type system dispatches. Reporting those would flag
# every port on the platform as dead, which is the opposite of true: they are the contracts
# everything else depends on.
#
# app/api and app/adapters are excluded from symbol-level reporting, and that is not laziness.
# A FastAPI route handler is never called by name — the decorator registers it. A port
# implementation is never called by name — the Protocol dispatches to it. Vulture sees no caller
# for either and is wrong both times, ~200 times per service. A gate that reports 200 pieces of
# correct code every run is one people stop reading, and then it protects nothing.
#
# Unused IMPORTS are reported everywhere, because that finding is precise wherever it appears.
SYMBOL_PATHS = ("app/core/",)

# 60, not 90, and this number is load-bearing. Vulture scores unused IMPORTS at 90 but unused
# functions, classes, methods and PROPERTIES at 60 — so a threshold of 90 silently excludes the
# entire category this gate was built for. The first version shipped at 90 and a planted dead
# function walked straight past it: the gate reported "no orphaned symbols" while one sat in the
# tree. Filtering by KIND (below) is what keeps the noise out, not the confidence number.
MIN_CONFIDENCE = "60"

# path-fragment -> why it is allowed to look dead. Every entry is a decision, not a mute button.
ALLOWLIST: dict[str, str] = {
    # The template ships code a generated service is expected to USE, so the scaffold itself
    # legitimately contains symbols nothing calls yet.
    "services/_template/": "the scaffold: its symbols are used by the services generated FROM it",

    # ── the replicated PII module ────────────────────────────────────────────────────────────
    # pii.py is copied verbatim into every service (self-contained by design), so `is_subject_ref`
    # is dead in the 17 services that only MINT pseudonyms and live in the one that also VALIDATES
    # them: audit-service calls it from erasure.assert_no_pii and pii_policy to refuse a raw
    # identity at write time. Splitting the module per service would mean 18 copies of a security
    # primitive drifting apart, which is worse than one unused function.
    "app/core/pii.py": "replicated verbatim per service; is_subject_ref is live in audit-service "
                       "(erasure.assert_no_pii + pii_policy) and unused where nothing validates",

    # ── reached only from tests/ ─────────────────────────────────────────────────────────────
    # Vulture scans app/ alone, so a pure function whose only caller is its unit test looks dead.
    # Each of these is a domain rule tested directly rather than through a route.
    "services/analytics-service/app/core/histogram.py":
        "mean() is exercised by tests/unit/test_sketches.py; vulture never scans tests/",
    "services/audit-service/app/core/erasure.py":
        "assert_no_pii is a write-time guard called from tests/unit/test_erasure_chain.py; the "
        "production caller is pii_policy, which reaches it through the same module",
    "services/billing-service/app/core/currency.py":
        "minor_per() is exercised by tests/unit/test_billing_v2.py",
    "services/billing-service/app/core/entitlements.py":
        "feature_enabled() is exercised by tests/unit/test_usage_entitlements.py",
    "services/billing-service/app/core/stores.py":
        "reconcile() is the balance invariant, asserted from tests/unit rather than a route",
    "services/compliance-service/app/core/models.py":
        "Consent.active() is asserted from tests/unit; the production path in consent_service "
        "re-states the same granted/expiry test inline because it must return WHICH reason "
        "(withdrawn vs expired), which a bool cannot carry",
    "services/compliance-service/app/core/privacy_dispatcher.py":
        "record_pushed_ack is driven from tests/unit/test_privacy_dispatcher.py",
    "services/connector-service/app/core/backoff.py":
        "parse_retry_after() is exercised by tests/unit/test_pure_cores.py",
    "services/file-service/app/core/file_service.py":
        "resolve_signed() is exercised by tests/unit/test_file_core.py",
    "services/file-service/app/core/upload_session.py":
        "list_for_tenant() is exercised by tests/unit/test_upload_session.py",
    "services/identity-service/app/core/refresh_store.py":
        "is_active() is exercised by tests/unit/test_erasure.py; the production path calls "
        "status() directly because it needs revoked-vs-unknown, not a bool",
    "services/tenant-service/app/core/tenant_repository.py":
        "get_by_domain() is exercised by tests/unit/test_tombstone.py",
    "services/wallet-service/app/core/funds.py":
        "apply() is exercised by tests/unit/test_wallet_core.py",

    # ── SUSPECTED MISSING CALL SITE — not dead, unreached. Kept deliberately. ─────────────────
    # These are not decoration: each is working code with a plausible caller that does not exist.
    # Deleting them would erase the evidence of the gap; wiring them is a behaviour change that
    # needs a decision, not a cleanup commit.
    "services/identity-service/app/core/membership.py":
        "SUSPECTED BUG — delete_person() erases the `persons` and `persons_by_identifier` rows and "
        "NOTHING calls it. IdentityErasure.erase() only deletes UserRepository's `users` rows, yet "
        "its receipt claims 'identity keeps NOTHING'. A DSAR erasure appears to leave the Person "
        "record and its identifier index behind. Needs a decision on subject->Person resolution "
        "and on memberships, so it is not wired here.",
    "services/stream-service/app/core/stores.py":
        "(The ExportStore gap is CLOSED — clip export is built: a ClipExporter port, an ffmpeg "
        "remux adapter, a queue-backed worker, and routes to request, poll and download. The "
        "store now has a creation path and EXPORT_MAX_RANGE_SECONDS is finally read.)",
    "services/billing-service/app/core/billing_service.py":
        "check_entitlement() is exercised by tests/unit/test_billing_v2 and test_grandfathering. "
        "(The pause()/reactivate() gap this used to record is CLOSED — both are routed and "
        "tested, and run_cycle now refuses to invoice a subscription that is not billable.)",
    "services/compliance-service/app/core/compliance_service.py":
        "is_held() is exercised by tests/unit/test_dsar_and_compliance (the production hold check "
        "calls _holds.active_for directly, because it needs WHICH hold for the refusal reason). "
        "(The delete_target()/delete_activity() gap is CLOSED — both are routed, tenant-scoped "
        "and audited.)",
}

_LINE = re.compile(r"^(?P<path>.+?):(?P<line>\d+): (?P<kind>unused \w+) '(?P<name>[^']+)'")


def scan(service: pathlib.Path) -> tuple[list[str], str]:
    """Findings for one service, using ITS venv — services are self-contained by design."""
    for candidate in (service / ".venv" / "Scripts" / "python.exe",
                      service / ".venv" / "bin" / "python"):
        if candidate.exists():
            python = candidate
            break
    else:
        return [], "no venv"

    # Scan the WHOLE app so usages are seen, then report only from REPORT_PATHS. Narrowing the
    # scan instead would narrow usage detection too: scoped to app/core alone, vulture calls
    # WalletErasure dead because the route that constructs it lives in app/api.
    proc = subprocess.run(
        [str(python), "-m", "vulture", "app", "--min-confidence", MIN_CONFIDENCE],
        cwd=service, capture_output=True, text=True, timeout=300)
    if "No module named vulture" in proc.stderr:
        # Honest skip, never a silent pass — the same rule the security gate learned the hard way.
        return [], "vulture not installed"

    findings = []
    for line in proc.stdout.splitlines():
        match = _LINE.match(line.strip())
        if not match or match.group("kind") not in REPORTED:
            continue
        rel = f"services/{service.name}/{match.group('path')}".replace("\\", "/")
        if any(frag in rel for frag in ALLOWLIST):
            continue
        if match.group("kind") != "unused import" and not any(p in rel for p in SYMBOL_PATHS):
            continue
        findings.append(f"{rel}:{match.group('line')}: {match.group('kind')} "
                        f"{match.group('name')!r}")
    return findings, ""


def main() -> int:
    problems: list[str] = []
    skipped: list[str] = []
    scanned = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        if not (service / "app").exists():
            continue
        # Only a WHOLE-DIRECTORY entry skips the scan. A file-level entry must not: substring
        # matching means "services/x/app/core/pii.py" contains "services/x/", so the original
        # test would have silently stopped scanning every service that allowlists a single file —
        # turning one suppressed symbol into a suppressed service. The per-finding filter in
        # scan() is what applies file-level entries.
        if f"services/{service.name}/" in ALLOWLIST:
            continue
        findings, skip = scan(service)
        if skip:
            skipped.append(f"{service.name} ({skip})")
            continue
        scanned += 1
        problems.extend(findings)

    if skipped:
        # Named, never silent: a service nobody scanned is a service nobody is checking.
        print(f"      skipped: {', '.join(skipped)}")

    if problems:
        print("      dead code — symbols nothing uses:")
        for p in problems:
            print(f"        {p}")
        print("      A symbol nobody calls is a rule that can rot: it can be edited, or become")
        print("      wrong, and no test will notice. Delete it, wire it up, or add an ALLOWLIST")
        print("      entry with a reason in tools/ci/check_dead_code.py.")
        return 1

    print(f"      {scanned} services carry no orphaned imports, functions, classes or properties")
    return 0


if __name__ == "__main__":
    sys.exit(main())
