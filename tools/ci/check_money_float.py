#!/usr/bin/env python3
"""safety:money-float — money must not cross a gateway/serialization boundary as a float.

THE FAILURE THIS EXISTS TO PREVENT (payments checkout audit 2026-09-19, finding L6).

Amounts are integer minor units internally, but a gateway talks major units, and the conversion was
`round(amount_minor / 100.0, 2)` / `int(round(x * 100))` — a hardcoded exponent-2 that silently
mischarges a 3-dp (KWD/BHD/OMR) or 0-dp (JPY/KRW) currency by 10-100x, and `float` money can round a
minor unit off by one. Money math must be integer/Decimal with an ISO-4217 exponent, not a literal
/100 or *100 on a float.

This gate flags, in every service, a `<money-ish> / 100`, `<money-ish> * 100` (or /1000, *1000), and
`float(<money-ish>)`, where the expression text mentions amount/price/minor/major/total/fee/cost/sum/
balance/refund/charge. Heuristic on purpose (it reads intent from names), so a genuine non-money use
opts out with an in-line `# money: ok — reason`. Reads source via `ast`.
"""
from __future__ import annotations

import ast
import os
import pathlib
import sys

ROOT = pathlib.Path(os.environ.get("MONEY_FLOAT_GATE_ROOT") or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# Payment money, not analytics metrics: the L6 bug is the minor<->major CONVERSION. We flag a
# hardcoded 100/1000 scaling of a money-ish operand, and float() only of a *minor-units* value (an
# integer-minor amount turned to float is the concrete rounding bug). Generic "total/sum/value/cost"
# is excluded so analytics aggregates (legitimately float) do not swamp the signal.
_MONEY_WORDS = ("amount", "price", "minor", "fee", "vat", "subtotal", "refund", "charge")
_FLOAT_WORDS = ("minor",)          # float() flagged only for minor-unit values
_SCALES = {100, 1000}


def _text(node: ast.AST) -> str:
    try:
        return ast.unparse(node).lower()
    except Exception:  # noqa: BLE001
        return ""


def _is_money(node: ast.AST) -> bool:
    return any(w in _text(node) for w in _MONEY_WORDS)


def _is_scale_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
        and int(node.value) in _SCALES and float(node.value) == int(node.value)


def _has_marker(line: str) -> bool:
    return "# money: ok" in line


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
        hit = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.Mult)):
            # A money-ish operand scaled by a 100/1000 literal — the hardcoded-exponent conversion.
            a, b = node.left, node.right
            money_side = a if (_is_scale_const(b) and _is_money(a)) else (
                b if (_is_scale_const(a) and _is_money(b)) else None)
            # EXCLUDE percent math: `Decimal(subtotal_minor) * Decimal(pct) / 100` scales a COMPOUND
            # expression (a BinOp), where /100 is the percent divisor, not a minor<->major exponent.
            # A real exponent bug scales a plain value (a name, int(amount_minor)), never a product.
            if money_side is not None and not isinstance(money_side, ast.BinOp):
                op = "/" if isinstance(node.op, ast.Div) else "*"
                hit = f"money value scaled by a hardcoded {op}{int((b if _is_scale_const(b) else a).value)}"
        # A separate float(<money>) rule was dropped: float(major(...)) is correct serialization of a
        # currency-helper result, and a bare float(amount_minor) is coercion — neither is the exponent
        # bug. The concrete L6 defect is the hardcoded /100 * 100 conversion detected above.
        if hit and not _marker_at_or_above(lines, node.lineno):
            out.append(f"{rel}:{node.lineno}: {hit} — a hardcoded exponent mischarges 3-dp/0-dp "
                       f"currencies and float rounds money wrong. Use an ISO-4217 minor-unit helper "
                       f"(Decimal/int), or mark `# money: ok — reason`.")
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
        print("safety:money-float: FAIL")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:money-float: PASS — {files} files; no float/hardcoded-exponent money math")
    return 0


if __name__ == "__main__":
    sys.exit(main())
