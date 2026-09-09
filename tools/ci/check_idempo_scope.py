#!/usr/bin/env python
"""Gate: an idempotency / dedupe record must be keyed by TENANT, not by the caller's key alone.

THE FOOTGUN THIS EXISTS FOR.

A dedupe store answers "have I already done this?" keyed on a CALLER-SUPPLIED idempotency_key (an
order number, an invoice id, a request id). If that key is written to a GLOBAL slot — the raw key
with no tenant in it — two tenants presenting the same key collide. The second caller is told
`duplicate` and handed the FIRST tenant's result: its own send never goes out (a dropped OTP), its
usage is never metered (real money billed to Meta but not to the customer — the cap never binds), or
it reads another tenant's payload. It compiles, passes its own single-tenant tests, and is a
cross-tenant correctness+money breach. Found by audit; now checked against the CODE.

THE RULE. For every datastore access (`.put` / `.get` / `.create` / `.set`) whose FIRST argument
resolves to a collection whose name matches the idempotency pattern (idempo|dedup), the KEY (second
positional argument) MUST be tenant-scoped:

  - a CALL expression — a scoping helper such as `_idempo_key(tenant, key)`; or
  - an expression that length-prefixes the tenant — it mentions `len(` (the `f"{len(tenant)}:..."`
    idiom that makes a delimiter-injection attack impossible).

A BARE name / attribute / plain constant as the key FAILS: that is the global slot this gate exists
to forbid. A row already carrying the tenant in a composite the store builds internally is fine —
this only inspects the KEY handed to the datastore.

PLUG-AND-PLAY: the collection-name pattern, the accessor method names, and reviewed exemptions are
all config, so a repo that names its dedupe collections or its scoping idiom differently points this
gate at itself.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# A collection whose NAME matches this is an idempotency/dedupe store and must be tenant-scoped.
_COLL_RE = re.compile(os.environ.get("IDEMPO_COLL_RE", r"idempo|dedup"), re.IGNORECASE)
# Datastore accessors whose 2nd positional arg is the key.
_ACCESSORS = frozenset(x.strip() for x in
                       os.environ.get("IDEMPO_ACCESSORS", "put,get,create,set,put_if_absent,cas").split(",")
                       if x.strip())
# The accessor must be called ON a datastore-like receiver — this is what keeps a `self._cfg.get(...)`
# config read (whose literal arg merely contains "idempo") from being mistaken for a store access.
_STORE_RECV_RE = re.compile(os.environ.get("IDEMPO_STORE_RECV_RE", r"store|datastore|_ds|repo|kv"))
# A key expression is tenant-scoped if it references a name/attr matching this (the tenant), OR is a
# call (a scoping helper), OR is a local name assigned from such an expression in the same function.
_TENANT_RE = re.compile(os.environ.get("IDEMPO_TENANT_RE", r"tenant|partition"), re.IGNORECASE)
# Reviewed exceptions: "service:collection" pairs that are legitimately global, or thin repository
# layers whose key is namespaced by the CALLER (named, comma-separated). Extend via env.
#   audit-service:audit_idempo — audit_repository.py is a generic thin layer; the idempotency key is
#     namespaced by (service, tenant) upstream in AuditService.record_idempotent before it reaches it.
_EXEMPT = {"audit-service:audit_idempo"}
_EXEMPT |= {x.strip() for x in os.environ.get("IDEMPO_EXEMPT", "").split(",") if x.strip()}


def _string_consts(tree: ast.AST) -> dict[str, str]:
    """Module-level NAME -> string literal, so a collection referenced by its constant resolves."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    out[t.id] = node.value.value
    return out


def _coll_name(arg: ast.expr, consts: dict[str, str]) -> str | None:
    """The collection name an accessor's first arg names — a literal, or a resolved constant."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.Name):
        return consts.get(arg.id)
    return None


def _expr_is_scoped(expr: ast.expr) -> bool:
    """True when the expression itself carries the tenant: any Call (a scoping helper like
    `_idempo_key(tenant, key)` or `f"{len(tenant)}..."`), or a reference to a tenant/partition
    name or attribute (`f"{tenant}|{op}|{source}"`)."""
    for node in ast.walk(expr):
        if isinstance(node, ast.Call):
            return True
        if isinstance(node, ast.Name) and _TENANT_RE.search(node.id):
            return True
        if isinstance(node, ast.Attribute) and _TENANT_RE.search(node.attr):
            return True
    return False


def _scoped_names(fn: ast.AST) -> set[str]:
    """Local names assigned from a scoped expression within one function — so a key built once into a
    variable (`idem_key = self._idempo_key(...)`) and then passed to the store still reads as scoped."""
    names: set[str] = set()
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign) and _expr_is_scoped(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and node.value is not None \
                and isinstance(node.target, ast.Name) and _expr_is_scoped(node.value):
            names.add(node.target.id)
    return names


def _is_store_recv(func: ast.Attribute) -> bool:
    """The accessor is called on a datastore-like receiver (self._store, self._ds, a repo, …), never
    a config/settings object — a `self._cfg.get('idempotency_ttl')` is not a store access."""
    try:
        recv = ast.unparse(func.value)
    except Exception:      # pragma: no cover — older ast without unparse
        return True
    return bool(_STORE_RECV_RE.search(recv))


def scan_service(root: pathlib.Path) -> list[str]:
    """Return a list of 'file:line collection' violations for one service (empty = clean)."""
    service = root.name
    app = root / "app"
    if not app.is_dir():
        return []
    bad: list[str] = []
    for path in sorted(app.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        consts = _string_consts(tree)
        # Map each store-access call node -> the set of scoped local names in its enclosing function.
        scoped_at: dict[int, set[str]] = {}
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                sc = _scoped_names(fn)
                for inner in ast.walk(fn):
                    if isinstance(inner, ast.Call):
                        scoped_at[id(inner)] = sc
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            if node.func.attr not in _ACCESSORS or len(node.args) < 2:
                continue
            if not _is_store_recv(node.func):
                continue
            coll = _coll_name(node.args[0], consts)
            if not coll or not _COLL_RE.search(coll):
                continue
            if f"{service}:{coll}" in _EXEMPT:
                continue
            key = node.args[1]
            scoped = _expr_is_scoped(key) or (
                isinstance(key, ast.Name) and key.id in scoped_at.get(id(node), set()))
            if not scoped:
                try:
                    rel = path.relative_to(ROOT).as_posix()
                except ValueError:      # a selftest tempdir outside the repo root
                    rel = path.as_posix()
                bad.append(f"{rel}:{node.lineno} {coll}")
    return bad


def main() -> int:
    violations: list[str] = []
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        violations.extend(scan_service(svc))
    if violations:
        print("safety:idempo-scope — dedupe record keyed WITHOUT a tenant (global slot; a cross-tenant")
        print("collision on a shared caller key). Scope the key: `_idempo_key(tenant, key)` /")
        print('`f"{len(tenant)}:{tenant}|{key}"`. Reviewed globals go in IDEMPO_EXEMPT.')
        for v in violations:
            print(f"  {v}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
