#!/usr/bin/env python
"""Gate: every credential-rotation path keeps the PREVIOUS secret across an overlap that EXPIRES ON
ITS OWN — never a hard cutover, and never an overlap only a human can end.

Rotating a machine credential is not like changing a password a person types next login. The
credential is EMBEDDED — in a POS build on hundreds of tills, in a partner's server config — and each
holder updates on its own schedule. A rotation that kills the old secret the instant it runs takes
the whole fleet offline until every holder happens to update (a fortnight for a shop open three days
a week), during which they fail silently, believing they still sync. And the opposite failure is just
as real: an overlap an operator must end by hand is an overlap nobody ends, so a credential still
works a year after it was "rotated".

So a rotation path must reference BOTH: a PREVIOUS-secret concept (an overlap exists) AND a
TIMESTAMP EXPIRY for it (it ends on its own). A new `rotate` method that has neither is the exact hard
cutover this gate exists to stop — the failure class that recurs whenever someone adds a rotation to
another service next year without remembering this one.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("ROTATION_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# A rotation method (def rotate / rotate_secret / rotate_*). The gate discovers these; a new one that
# is not one of the two established patterns fails until a human classifies it.
_ROTATE = re.compile(r"^rotate(_|$)")

# Markers, checked over the whole file the method lives in (the model, the store, and the method are
# usually co-located). A PREVIOUS concept AND a TIMESTAMP-EXPIRY concept must both be present.
_PREV = ("prev_", "previous", "overlap", "rotation_window")
_EXPIRES = ("_expires", "_until", "overlap_seconds", "rotation_window", "_window")

# Rotation methods that legitimately cannot carry a timestamp-expiring overlap yet — each with a
# reason a person can read and challenge. Env-augmentable for the self-test.
EXCLUDED: dict[str, str] = {
    "webhook-service:rotate_secret":
        "keeps the previous signing secret (ep.secrets = [fresh, prev]) so in-flight deliveries still "
        "verify — an overlap EXISTS — but drops it on the NEXT rotation rather than by a timestamp, so "
        "an unrotated old secret can outlive its usefulness. Adding per-secret expiry is a tracked "
        "follow-up; the keep-2 overlap already prevents the hard-cutover outage this gate's worst case "
        "describes",
}
for _e in (os.environ.get("ROTATION_GATE_EXCLUDE", "") or "").split(","):
    if _e.strip():
        EXCLUDED[_e.strip()] = "excluded via env (selftest)"


def _rotation_methods(path: pathlib.Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return []
    out: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and _ROTATE.match(node.name):
            out.append(node.name)
    return out


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    checked = excused = 0
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        core = svc / "app" / "core"
        if not core.is_dir():
            continue
        for path in sorted(core.rglob("*.py")):
            methods = _rotation_methods(path)
            if not methods:
                continue
            text = path.read_text(encoding="utf-8")
            has_prev = any(m in text for m in _PREV)
            has_exp = any(m in text for m in _EXPIRES)
            for method in methods:
                checked += 1
                tag = f"{svc.name}:{method}"
                if tag in EXCLUDED:
                    excused += 1
                    continue
                if not (has_prev and has_exp):
                    missing = []
                    if not has_prev:
                        missing.append("a PREVIOUS-secret overlap")
                    if not has_exp:
                        missing.append("a TIMESTAMP expiry for the overlap")
                    problems.append(
                        f"{tag} ({path.relative_to(ROOT)}): a credential rotation with no "
                        f"{' and no '.join(missing)}. A hard cutover takes an embedded-credential "
                        f"fleet offline until every holder updates; an unexpiring overlap never ends. "
                        f"Keep the previous secret and expire it by timestamp (see service_accounts."
                        f"rotate), or add {tag!r} to EXCLUDED with a reason.")
    if problems:
        print(f"safety:rotation-overlap — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"safety:rotation-overlap OK — {checked} credential-rotation path(s) keep and time-expire "
          f"the previous secret ({excused} excused with a reason); no hard cutover")
    return 0


if __name__ == "__main__":
    sys.exit(main())
