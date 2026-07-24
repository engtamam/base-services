#!/usr/bin/env python3
"""safety:backup-coverage — a collection the code writes but the manifest does not declare is data
that a backup run will silently skip.

This is the guarantee that makes backup plug-and-play. A service is backed up by declaring what it
holds and answering two endpoints; the backup service never opens another service's database. That
only works if the declaration is COMPLETE — and "complete" cannot be a promise, because the failure
it protects against is exactly the kind nobody notices: a new collection is added, the backup
manifest is not updated, backups keep running green, and the day a restore is needed that data is
simply not there.

So completeness is mechanical. This gate discovers every collection the code actually writes and
checks each one is accounted for in the manifest's `backup` block — either as something backed up,
or as something DELIBERATELY excluded with a reason. A collection that is neither fails the build.

    every collection the code writes  ⊆  backup.collections ∪ backup.excluded

WHY EXCLUSION EXISTS. Not everything is worth restoring. Idempotency keys, rate-limit counters and
short-lived caches are regenerated, and backing them up would bloat every archive with data whose
restoration means nothing. But exclusion must be a DECISION, written down with a reason — the same
shape as the scan gate's `# sweeper:` marker and the dead-code allowlist. An excluded collection is
a sentence somebody can read and challenge; a forgotten one is a silent hole.

HOW COLLECTIONS ARE DISCOVERED. Read as source, never by importing — a gate that runs what it
audits is not a gate. Two forms cover how this codebase names a collection:

  * `collection = "invoices"` — the class attribute on a store, the dominant pattern.
  * a string passed as the first argument to a datastore method (`put/get/find/scan_all/delete/
    list`), directly or through a simple module-level constant (`_INVOICES = "invoices"`).

WHAT IT DOES NOT DO. It does not check the export/import endpoints exist — that is 46.2's contract
gate. And a service with NO `backup` block at all is reported as NOT ONBOARDED (a note), not failed:
during rollout that is a real, honest state, and a service becomes strictly checked the moment it
declares its first backup block. Once every service is onboarded, the note becomes the failure.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = pathlib.Path(os.environ.get("SERVICES_ROOT") or (ROOT / "services"))

# The datastore methods whose first string argument names a collection.
_DS_METHODS = frozenset({"put", "get", "find", "scan_all", "delete", "list"})

# The receiver MUST be a datastore, or every `dict.get("field")` and `event.get("type")` in the
# service looks like a collection access. Matching by receiver name is what keeps this from
# reporting a hundred field names — the same "cries wolf" failure the event-contract and
# admin-params gates each hit on their first draft.
# NOT bare "s" or "ds": those are loop variables — `for s in subscriptions: s.get("kind")` is not a
# datastore access, and including them put every dict field into the discovered set. Only names that
# actually denote a store in this codebase.
_DS_RECEIVERS = frozenset({"store", "_store", "_s", "datastore", "_datastore", "_ds"})

# Collections that belong to the platform's own machinery, present in every service, and are
# never a service's own data to declare. Excluded from discovery so the gate does not force every
# service to declare the same handful of infrastructure tables.
_INFRA = frozenset({
    "runtime_config",       # Phase 44 operator overrides
    "operator_secrets",     # Phase 44 sealed credentials (their own backup story: they are re-enterable)
})

_SKIP_DIRS = ("__pycache__", ".venv", "tests", "node_modules")


def _py_files(service: pathlib.Path):
    for sub in ("core", "adapters"):
        base = service / "app" / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*.py"):
            if not any(part in _SKIP_DIRS for part in p.parts):
                yield p


def _is_datastore(node: ast.expr) -> bool:
    """Is this call receiver a datastore? `self._store`, `store`, `self._s` — not an arbitrary dict.

    This is what keeps the gate from reporting every `event.get("type")` and `row.get("tenant")` as
    a collection. The receiver name is the signal; matching any `.get()`/`.find()` was the first
    draft and it reported a hundred field names, the same cries-wolf failure the event-contract and
    admin-params gates each hit before being narrowed.
    """
    if isinstance(node, ast.Attribute):
        return node.attr in _DS_RECEIVERS
    if isinstance(node, ast.Name):
        return node.id in _DS_RECEIVERS
    return False


def _written_collections(service: pathlib.Path) -> set[str]:
    """Every collection name the service's code reads or writes.

    Two precise signals only: the `collection = "..."` class attribute, and a string reaching a
    DATASTORE method's first argument — directly, through a module/class constant, or through a
    `self._UPPER` / `self.collection` attribute.
    """
    found: set[str] = set()
    for path in _py_files(service):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        # String constants worth resolving: `_UPPER = "..."` and `collection = "..."`.
        consts: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                    and isinstance(node.value.value, str) and node.value.value:
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and (tgt.id.isupper()
                                                      or tgt.id == "collection"
                                                      or tgt.id.startswith("_")):
                        consts[tgt.id] = node.value.value
                        if tgt.id == "collection":
                            found.add(node.value.value)   # the canonical declaration
        # A string reaching a datastore method's first argument.
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in _DS_METHODS or not node.args:
                continue
            if not _is_datastore(node.func.value):
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                found.add(consts[arg.id])
            elif isinstance(arg, ast.Attribute) and arg.attr in consts:
                found.add(consts[arg.attr])          # self.collection, self._SEQ, self._BALANCES
    return {c for c in found if c and c not in _INFRA}


def _backup_block(service: pathlib.Path) -> dict | None:
    """The manifest's backup block, read from app/backup_manifest.py as a literal.

    A separate module, like admin_actions.py and auditable_actions.py, so it is a pure declaration
    read with literal_eval rather than executed.
    """
    path = service / "app" / "backup_manifest.py"
    if not path.is_file():
        return None
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        target = None
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            target, value = node.target.id, node.value
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            target, value = node.targets[0].id, node.value
        if target == "BACKUP" and value is not None:
            try:
                return ast.literal_eval(value)
            except ValueError:
                return {}
    return None


def _declared(block: dict) -> tuple[set[str], set[str]]:
    backed = {c.get("name") for c in block.get("collections", []) if c.get("name")}
    excluded = {c.get("name") for c in block.get("excluded", []) if c.get("name")}
    return backed, excluded


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []
    onboarded = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        if service.name.startswith("_"):
            continue
        written = _written_collections(service)
        block = _backup_block(service)
        if block is None:
            if written:
                notes.append(f"{service.name}: not onboarded to backup "
                             f"({len(written)} collection(s) written, none declared)")
            continue
        onboarded += 1
        backed, excluded = _declared(block)

        # Every excluded collection must carry a reason — an exclusion nobody can read is a hole
        # with a lid on it.
        for entry in block.get("excluded", []):
            if not entry.get("reason"):
                problems.append(f"{service.name}: excludes {entry.get('name')!r} from backup with "
                                f"no reason — an exclusion must be a decision somebody can challenge")

        undeclared = written - backed - excluded
        for col in sorted(undeclared):
            problems.append(
                f"{service.name}: writes collection {col!r} but the backup manifest neither backs "
                f"it up nor excludes it — a backup run will silently skip it, and it will be "
                f"missing the day a restore is needed")

        # A declared collection the code does not write is a promise to back up nothing: not
        # dangerous the way the reverse is, but it means an archive reserves space for data that
        # never arrives, and it usually signals a renamed or deleted collection.
        for col in sorted((backed | excluded) - written - _INFRA):
            notes.append(f"{service.name}: declares collection {col!r} that the code does not "
                         f"write — renamed or removed?")

    for n in notes:
        print(f"      note: {n}")
    if problems:
        print("      backup coverage is incomplete:")
        for p in problems:
            print(f"        {p}")
        print("      A collection that is neither backed up nor deliberately excluded is a hole.")
        return 1

    print(f"      {onboarded} service(s) onboarded to backup; every written collection is declared")
    return 0


if __name__ == "__main__":
    sys.exit(main())
