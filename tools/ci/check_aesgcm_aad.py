#!/usr/bin/env python3
"""safety:aesgcm-aad — an AEAD seal/open must BIND its ciphertext to its slot with associated data.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding M1).

AES-GCM (and any AEAD) authenticates the ciphertext, but NOT where it lives. Sealing with
`associated_data=None` means a stored blob decrypts under the key no matter which row it sits in — so
a database tamperer with NO key can COPY tenant A's sealed credential into tenant B's row (or under a
different name/slot) and B silently authenticates as A. The whole promise of the sealed credential
store ("a stolen/tampered database yields nothing usable") is void without an AAD that binds the blob
to its `(collection, tenant, name)` identity, so a relocated or renamed blob fails the tag loudly.

This gate flags, in every service, any `AESGCM(...).encrypt(...)` / `.decrypt(...)` whose associated
data is missing or literally `None`. It reads source via `ast` — a gate that imports what it audits is
not a gate.

A call where binding is genuinely not applicable opts out with an in-line `# aad: ok — reason` on the
call (or a contiguous comment directly above it).
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("AESGCM_AAD_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
_AEAD_CTORS = {"AESGCM", "AESCCM", "ChaCha20Poly1305", "AESGCMSIV", "AESOCB3"}
_SEAL_METHODS = {"encrypt", "decrypt"}


def _is_aead_ctor(node: ast.AST) -> bool:
    """True if `node` is a call constructing an AEAD cipher, e.g. AESGCM(key) or module.AESGCM(key)."""
    if not isinstance(node, ast.Call):
        return False
    fn = node.func
    name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else "")
    return name in _AEAD_CTORS


def _has_marker(line: str) -> bool:
    return "# aad: ok" in line


def _marker_at_or_above(src_lines: list[str], lineno: int) -> bool:
    if lineno - 1 < len(src_lines) and _has_marker(src_lines[lineno - 1]):
        return True
    i = lineno - 2
    while i >= 0:
        stripped = src_lines[i].strip()
        if not stripped:
            i -= 1
            continue
        if not stripped.startswith("#"):
            return False
        if _has_marker(stripped):
            return True
        i -= 1
    return False


def _aad_arg(call: ast.Call) -> ast.AST | None:
    """The associated-data argument of an AEAD encrypt/decrypt call: positional index 2, or the
    `associated_data=` keyword. Returns the arg node, or None if it is absent."""
    if len(call.args) >= 3:
        return call.args[2]
    for kw in call.keywords:
        if kw.arg == "associated_data":
            return kw.value
    return None


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    src_lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in _SEAL_METHODS:
            continue
        if not _is_aead_ctor(node.func.value):   # receiver must be an AEAD ctor: AESGCM(key).encrypt(...)
            continue
        aad = _aad_arg(node)
        missing = aad is None or (isinstance(aad, ast.Constant) and aad.value is None)
        if missing and not _marker_at_or_above(src_lines, node.lineno):
            out.append(f"{rel}:{node.lineno}: AEAD {node.func.attr}() with no associated_data (None) — "
                       f"the sealed blob is not bound to its (collection, tenant, name) slot, so a "
                       f"tampered DB can relocate it across rows/tenants. Pass an AAD binding its "
                       f"identity, or mark `# aad: ok — reason`.")
    return out


def _iter_targets():
    """Every service's app/**/*.py, plus the template (the source of copied crypto)."""
    if not SERVICES.exists():
        return
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir():
            continue
        appdir = svc / "app"
        if not appdir.exists():
            continue
        for py in sorted(appdir.rglob("*.py")):
            yield py


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems: list[str] = []
    files = 0
    for py in _iter_targets():
        files += 1
        problems.extend(_check_file(py, str(py.relative_to(ROOT))))
    if problems:
        print("safety:aesgcm-aad: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:aesgcm-aad: PASS — {files} files; every AEAD seal/open binds associated data")
    return 0


if __name__ == "__main__":
    sys.exit(main())
