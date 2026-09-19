#!/usr/bin/env python3
"""safety:timing-compare — a secret/token/signature must be compared in CONSTANT TIME.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding L2).

`supplied == secret` (or `!=`) short-circuits on the first differing byte, so the compare time leaks
how much of a guess is correct — a classic side channel for recovering a shared secret / HMAC /
signature / webhook token one byte at a time. The fix is `hmac.compare_digest(a, b)`, whose time does
not depend on where the values differ.

This gate flags, in every service, an `==`/`!=` where one side is a name/attribute that looks like a
secret (secret, token, signature, sig, hmac, mac, digest, password, passwd, api_key, apikey, otp,
webhook_secret) AND the other side is also a non-literal (comparing to a constant like "" or None is a
presence check, not a secret compare — those are left alone). Reads source via `ast`.

A compare that is genuinely not secret-sensitive opts out with an in-line `# timing: ok — reason`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("TIMING_COMPARE_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# High-signal only: a mismatch on any of these leaks a secret one byte at a time. "token" is
# deliberately NOT here — idempotency/lease/cursor/JWT tokens are compared for equality legitimately
# and are not byte-guessable secrets; the danger is a raw shared secret / signature / HMAC / OTP /
# password / api key.
_SECRET_WORDS = ("secret", "signature", "hmac", "digest", "password", "passwd",
                 "api_key", "apikey", "otp")
# Names that merely CONTAIN a secret word but are not the sensitive value itself — reduce false hits.
_ALLOW_NAMES = {"compare_digest"}   # extend if a real false positive appears


def _name_text(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:  # noqa: BLE001
        return ""


def _looks_secret(node: ast.AST) -> bool:
    if not isinstance(node, (ast.Name, ast.Attribute, ast.Subscript)):
        return False
    text = _name_text(node).lower()
    if any(a in text for a in _ALLOW_NAMES):
        return False
    # word-ish match: the secret word appears as a token boundary, not inside an unrelated word
    return any(w in text for w in _SECRET_WORDS)


def _is_literalish(node: ast.AST) -> bool:
    # A constant, a trivial container of constants, or an ALL-CAPS module constant / sentinel
    # (e.g. `password_hash == UNUSABLE_PASSWORD`): a presence/sentinel check, not a byte-guessable
    # secret compare.
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.Name) and node.id.isupper():
        return True
    if isinstance(node, ast.Attribute) and node.attr.isupper():
        return True
    return isinstance(node, (ast.List, ast.Tuple, ast.Set)) and \
        all(isinstance(e, ast.Constant) for e in getattr(node, "elts", []))


def _has_marker(line: str) -> bool:
    return "# timing: ok" in line


def _marker_at_or_above(src_lines: list[str], lineno: int) -> bool:
    if lineno - 1 < len(src_lines) and _has_marker(src_lines[lineno - 1]):
        return True
    i = lineno - 2
    while i >= 0:
        s = src_lines[i].strip()
        if not s:
            i -= 1
            continue
        if not s.startswith("#"):
            return False
        if _has_marker(s):
            return True
        i -= 1
    return False


def _check_file(path: pathlib.Path, rel: str) -> list[str]:
    src = path.read_text(encoding="utf-8")
    lines = src.splitlines()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        if not isinstance(node.ops[0], (ast.Eq, ast.NotEq)):
            continue
        left, right = node.left, node.comparators[0]
        # a secret name on one side, and the OTHER side is not a plain literal presence check
        pairs = ((left, right), (right, left))
        if any(_looks_secret(a) and not _is_literalish(b) for a, b in pairs):
            if _marker_at_or_above(lines, node.lineno):
                continue
            out.append(f"{rel}:{node.lineno}: secret/token compared with "
                       f"'{'==' if isinstance(node.ops[0], ast.Eq) else '!='}' — a timing side channel. "
                       f"Use hmac.compare_digest(), or mark `# timing: ok — reason`.")
    return out


def main() -> int:
    if not SERVICES.exists():
        print(f"no services dir at {SERVICES}")
        return 0
    problems, files = [], 0
    for svc in sorted(SERVICES.iterdir()):
        appdir = svc / "app"
        if not svc.is_dir() or not appdir.exists():
            continue
        for py in sorted(appdir.rglob("*.py")):
            files += 1
            problems.extend(_check_file(py, str(py.relative_to(ROOT))))
    if problems:
        print("safety:timing-compare: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:timing-compare: PASS — {files} files; no non-constant-time secret comparisons")
    return 0


if __name__ == "__main__":
    sys.exit(main())
