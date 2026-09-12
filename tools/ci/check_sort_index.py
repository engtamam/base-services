#!/usr/bin/env python3
"""safety:sort-index — every keyset sort field must have a matching postgres index.

THE FAILURE THIS EXISTS TO PREVENT (fleet audit 2026-09-12, verification-service-03).

The generic postgres DataStore orders a find() by `COALESCE(value->'<field>','null'::jsonb)`. That is
correct for any field, but only O(page) when a btree index exists on the SAME expression; without one
Postgres bitmap-scans every matching row and top-N sorts them — O(tenant-rows), the exact slow path
the adapter's own comment warns about. verification-service sorted every query by `created_at` while
the only index was on `created`, so the index was never used.

This gate reads, per service, WITHOUT importing anything:
  • the jsonb order fields the postgres adapter indexes — `value->'<field>'` inside its _INDEXES, and
  • every `sort=("<field>", ...)` literal used in the service's app/ code.
Each sort field that is not indexed (and is not a structurally-ordered key) is a finding.

Read as source via ast/regex — a gate that imports what it audits is not a gate.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("SORT_INDEX_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# `id`/`key` are the datastore's structural tie-break columns (always in the ORDER BY and PK), never a
# value-> field, so they never need a jsonb index. A service may add more via a `# sort-index: ok
# <field> — reason` marker in its postgres.py.
_STRUCTURAL = {"id", "key"}

# Copied infra whose sorts are over a BOUNDED or filter-narrowed, rare set — not a growing request
# listing: `secret_store.py` is a per-service handful of secrets; `erasure.py` is a DSAR path bounded
# by (tenant, subject) and run rarely. Skipped structurally rather than tagged in ~20 copied lines.
_SKIP_FILES = {"secret_store.py", "erasure.py"}

_INDEX_FIELD = re.compile(r"value->'([A-Za-z0-9_]+)'")
_SORT_LITERAL = re.compile(r"""sort\s*=\s*\(\s*["']([A-Za-z0-9_]+)["']""")
_ALLOW = re.compile(r"#\s*sort-index:\s*ok\s+([A-Za-z0-9_]+)")


def _indexed_fields(pg: pathlib.Path) -> set[str]:
    text = pg.read_text(encoding="utf-8")
    fields = set(_INDEX_FIELD.findall(text))
    fields |= set(_ALLOW.findall(text))          # explicit per-service exemptions, documented in-place
    return fields


def _sort_fields(app_dir: pathlib.Path) -> dict[str, list[str]]:
    """field -> the files that sort by it (app code only, tests excluded)."""
    used: dict[str, list[str]] = {}
    for py in app_dir.rglob("*.py"):
        if py.name in _SKIP_FILES:
            continue
        rel = str(py.relative_to(ROOT))
        for field in _SORT_LITERAL.findall(py.read_text(encoding="utf-8")):
            used.setdefault(field, []).append(rel)
    return used


def _check_service(svc: pathlib.Path) -> list[str]:
    pg = svc / "app" / "adapters" / "datastore" / "postgres.py"
    app_dir = svc / "app"
    if not pg.exists() or not app_dir.exists():
        return []
    indexed = _indexed_fields(pg) | _STRUCTURAL
    problems: list[str] = []
    for field, files in sorted(_sort_fields(app_dir).items()):
        if field not in indexed:
            where = files[0]
            problems.append(
                f"{svc.name}: sort=('{field}', ...) at {where} has no matching index in "
                f"postgres.py (add a btree on COALESCE(value->'{field}',...), or a "
                f"`# sort-index: ok {field} — reason` marker if the collection is bounded).")
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
        checked += 1
        problems.extend(_check_service(svc))
    if problems:
        print("safety:sort-index: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:sort-index: PASS — {checked} services; every keyset sort field is indexed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
