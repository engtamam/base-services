#!/usr/bin/env python3
"""safety:keyed-integrity — a tamper-evidence/integrity driver must default to a KEYED variant.

THE FAILURE THIS EXISTS TO PREVENT (fleet audit 2026-09-12, audit-service-03).

audit-service's INTEGRITY_DRIVER defaulted to an UNKEYED sha256 hash-chain. A hash chain is
tamper-evident only against someone who cannot recompute the hashes — but the threat model for an
audit log IS an attacker with backend/database access, who can delete their own events and re-anchor
the whole chain with public sha256 so verify() passes clean. Only an HMAC keyed with a secret held
OUTSIDE the datastore resists that, and it was merely opt-in.

This gate enforces that any config field selecting an integrity/tamper-evidence driver does NOT
default to a known-unkeyed option — it must default to a keyed one (so a deploy fails closed without
a key rather than running an unprovable log). A service opts a field out with an in-line
`# keyed-integrity: ok <FIELD> — reason` marker.

Read as source via ast — a gate that imports what it audits is not a gate.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("KEYED_INTEGRITY_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# Config fields that SELECT a tamper-evidence / integrity chain driver. Matched by name.
_FIELD_RE = re.compile(r"INTEGRITY_DRIVER|.*_INTEGRITY_DRIVER|CHAIN_DRIVER|TAMPER_.*_DRIVER")
# Default values that provide NO keyed protection.
_UNKEYED = {"hash-chain", "sha256", "sha256-chain", "none", "plain", ""}
_ALLOW = re.compile(r"#\s*keyed-integrity:\s*ok\s+([A-Za-z0-9_]+)")


def _check(cfg: pathlib.Path) -> list[str]:
    text = cfg.read_text(encoding="utf-8")
    exempt = set(_ALLOW.findall(text))
    tree = ast.parse(text)
    problems: list[str] = []
    for node in ast.walk(tree):
        # class-body annotated assignment: NAME: type = "default"
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            name = node.target.id
            if not _FIELD_RE.fullmatch(name) or name in exempt:
                continue
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                if node.value.value.strip().lower() in _UNKEYED:
                    problems.append(
                        f"{name} defaults to {node.value.value!r} — an UNKEYED integrity driver is "
                        f"forgeable by anyone with datastore access. Default to a keyed variant "
                        f"(e.g. hmac-chain), or add `# keyed-integrity: ok {name} — reason`.")
    return problems


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems: list[str] = []
    checked = 0
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir() or svc.name.startswith("_"):
            continue
        cfg = svc / "app" / "config.py"
        if not cfg.exists():
            continue
        checked += 1
        for p in _check(cfg):
            problems.append(f"{svc.name}: {p}")
    if problems:
        print("safety:keyed-integrity: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:keyed-integrity: PASS — {checked} services; no integrity driver defaults to unkeyed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
