#!/usr/bin/env python
"""Gate: every service either serves /internal/backup/export, or is EXEMPT with a reason.

backup-service collects archives by calling each target's /internal/backup/export. A service that
does not serve it answers 404 and is silently un-backed-up — the run reports `missed` (correctly),
but the data a customer would actually lose (establishments, accounts, roles, balances, consent,
the audit log) is simply not captured. That is the kind of hole nobody sees until a restore is
needed. safety:backup-coverage checks completeness WITHIN a service that already backs up; this
checks the prior question — whether the service backs up AT ALL.

So every non-template service must DECLARE `/internal/backup/export` in its manifest, or appear in
EXEMPT with a reason a person can read and challenge. A brand-new service generated from _template
inherits the endpoint, so this can only regress by deliberate removal — and a legacy service that
predates it stays a loud, listed backlog item rather than a silent gap.

EXEMPT is not "we forgot" — it is a decision. `stateless` services (a pure proxy) hold nothing to
back up. `backfill-pending` names a service that SHOULD back up and has not been onboarded yet; each
is tracked, and onboarding it (add core/backup.py + the endpoints + a backup_manifest declaring its
collections) removes its line here.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("BACKUP_EXPORT_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# "<service>": "reason". Two kinds: stateless (permanent) and backfill-pending (a tracked backlog —
# the intent is to onboard these; the line goes away when the endpoint lands).
EXEMPT: dict[str, str] = {
    "api-gateway": "stateless edge proxy — holds no data of its own to back up",
    # backfill-pending — SHOULD serve /internal/backup/export; tracked for onboarding. The
    # data-critical ones (tenant/identity/authz/wallet/compliance/audit) go first.
    **{s: "backfill-pending — backup export not yet onboarded (tracked)" for s in (
        "analytics-service", "assistant-service", "audit-service", "authz-service",
        "compliance-service", "connector-service", "feature-flag-service", "file-service",
        "job-service", "notification-service", "onboarding-service",
        "search-service", "stream-service", "verification-service",
        "wallet-service", "webhook-service", "zatca-service")},
}
for _e in (os.environ.get("BACKUP_EXPORT_GATE_EXCLUDE", "") or "").split(","):
    if _e.strip():
        EXEMPT[_e.strip()] = "excluded via env (selftest)"


def _serves_export(svc: pathlib.Path) -> bool:
    man = svc / "app" / "manifest.py"
    if not man.is_file():
        return False
    try:
        tree = ast.parse(man.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            d = {k.value: v.value for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
            if d.get("path") == "/internal/backup/export" and d.get("method") == "POST":
                return True
    return False


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    serving = exempt = 0
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template"):
        if not (svc / "app" / "manifest.py").is_file():
            continue
        if _serves_export(svc):
            serving += 1
        elif svc.name in EXEMPT:
            exempt += 1
        else:
            problems.append(
                f"{svc.name}: does not serve /internal/backup/export and is not EXEMPT. A backup run "
                f"will silently skip its data. Onboard backup (copy _template's core/backup.py + the "
                f"backup endpoints + a backup_manifest declaring its collections), or add it to EXEMPT "
                f"with a reason.")
    # A stale exemption for a service that now DOES serve export should be removed.
    for name in sorted(EXEMPT):
        svc = SERVICES / name
        if svc.is_dir() and _serves_export(svc):
            problems.append(f"{name}: is EXEMPT but now serves /internal/backup/export — remove the "
                            f"stale exemption.")
    if problems:
        print(f"safety:backup-export — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:backup-export OK — {serving} service(s) serve /internal/backup/export, "
          f"{exempt} exempt with a reason ({sum(1 for r in EXEMPT.values() if 'backfill' in r)} "
          f"backfill-pending); no silent gap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
