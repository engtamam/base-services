#!/usr/bin/env python3
"""Prove safety:audit-wiring can actually FAIL.

A gate nobody has watched fail is decoration. This one guards a defect that already shipped twice
in this repo — code that is complete, correct and connected to nothing — so "it passes" tells us
nothing until we have watched it reject each thing it claims to reject.

Builds a throwaway repo per case, runs the real checker against it, and asserts the outcome.
"""
from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_audit_wiring.py"

_DECL = '''from typing import Any

AUDITABLE_ACTIONS: list[dict[str, Any]] = {actions}
'''

# A three-segment name. validate() allows it (it requires only a dot), so the emit matcher must
# see its record() call too. The first version matched exactly two segments and reported this as
# never-emitted — reported by a downstream user running v0.6.3, whose four correctly-wired
# multi-part actions all came back as violations.
_MULTIPART = [
    {"action": "catalog.draft.approved", "source": "admin", "critical": True, "resource": "draft",
     "why": "approving a draft publishes it to every tenant"},
]

_GOOD = [
    {"action": "thing.created", "source": "user", "critical": False, "resource": "thing"},
    {"action": "permission.granted", "source": "admin", "critical": True, "resource": "grant",
     "why": "an unattributable grant is indistinguishable from an attacker's"},
]


def _repo(tmp: pathlib.Path, *, actions, driver: str | None, emit: bool = True) -> pathlib.Path:
    """A minimal repo the checker can walk: one service, its declaration, its values file."""
    svc = tmp / "services" / "x-service" / "app"
    svc.mkdir(parents=True)
    (tmp / "services" / "_template" / "app").mkdir(parents=True)
    if actions is not None:
        (svc / "auditable_actions.py").write_text(_DECL.format(actions=actions), encoding="utf-8")

    # A route file that emits every critical action, unless the case is testing the absence of it.
    if actions and emit:
        calls = "\n".join(
            '    await request.app.state.auditor.record("%s")' % a["action"]
            for a in actions if isinstance(a, dict) and a.get("critical"))
        (svc / "routes.py").write_text(
            "async def handler(request):\n" + (calls or "    pass") + "\n", encoding="utf-8")

    values = tmp / "deploy" / "services"
    values.mkdir(parents=True)
    body = "service:\n  name: x-service\n"
    if driver is not None:
        body += f'env:\n  AUDIT_SINK_DRIVER: "{driver}"\n'
    (values / "x-service.values.yaml").write_text(body, encoding="utf-8")

    # The checker locates the repo from its own path, so it has to live inside the fake one.
    tools = tmp / "tools" / "ci"
    tools.mkdir(parents=True)
    shutil.copy(CHECK, tools / CHECK.name)
    return tools / CHECK.name


def _run(check: pathlib.Path) -> tuple[int, str]:
    r = subprocess.run([sys.executable, str(check)], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


CASES = [
    # (name, actions, driver, must_fail, expected_fragment)
    ("wired correctly", _GOOD, "http", False, "declare auditable action"),
    ("R1 declares but cannot emit", _GOOD, "null", True, "nowhere to send it"),
    ("R1 declares but no driver at all", _GOOD, None, True, "nowhere to send it"),
    ("R2 emits but declares nothing", None, "http", True, "declares NO auditable actions"),
    ("no audit trail at all", None, None, True, "no audit trail at all"),
    ("R3 critical action nothing emits", _GOOD, "http", True, "nothing ever"),
    # A dotted name with THREE segments must be recognised as emitted, not reported missing.
    ("multi-part action name is seen", _MULTIPART, "http", False, "declare auditable action"),
    ("R4 flat action name", [{"action": "created", "source": "user", "critical": False}],
     "http", True, "dotted"),
    ("R4 unknown source", [{"action": "a.b", "source": "panel", "critical": False}],
     "http", True, "source"),
    ("R4 criticality not a boolean", [{"action": "a.b", "source": "user", "critical": "yes"}],
     "http", True, "explicitly"),
    ("R4 critical with no why", [{"action": "a.b", "source": "user", "critical": True}],
     "http", True, "no 'why'"),
    ("R4 duplicate action", _GOOD + [dict(_GOOD[0])], "http", True, "twice"),
]


def main() -> int:
    failures = []
    for name, actions, driver, must_fail, fragment in CASES:
        with tempfile.TemporaryDirectory() as td:
            # The dedicated no-emit case builds its repo without the route file.
            check = _repo(pathlib.Path(td), actions=actions, driver=driver,
                          emit="nothing emits" not in name)
            rc, out = _run(check)
        failed = rc != 0
        if failed != must_fail:
            failures.append(f"  {name}: expected {'FAIL' if must_fail else 'PASS'}, "
                            f"got {'FAIL' if failed else 'PASS'}\n{out}")
        elif fragment not in out:
            failures.append(f"  {name}: right verdict, wrong reason — {fragment!r} not in:\n{out}")

    # A literal the gate cannot parse must be reported, never silently skipped: an unparseable
    # declaration that counted as "no problems" would be the perfect place to hide an empty one.
    with tempfile.TemporaryDirectory() as td:
        tmp = pathlib.Path(td)
        check = _repo(tmp, actions=_GOOD, driver="http")
        decl = tmp / "services" / "x-service" / "app" / "auditable_actions.py"
        # repr() renders the literal with SINGLE quotes — a substitution written for double quotes
        # matches nothing, the file stays valid, and the case silently proves the opposite of what
        # it claims. Asserting the edit landed is what stops this test from testing nothing.
        original = decl.read_text(encoding="utf-8")
        mutated = original.replace("'thing.created'", 'f"thing.{1 + 1}"')
        assert mutated != original, "selftest bug: the computed-literal mutation did not apply"
        decl.write_text(mutated, encoding="utf-8")
        rc, out = _run(check)
        if rc == 0 or "pure literal" not in out:
            failures.append(f"  computed literal was not rejected:\n{out}")

    if failures:
        print("audit-wiring selftest FAILED — the gate does not catch what it claims:")
        print("\n".join(failures))
        return 1
    # Not "every one rejected": some cases assert the gate PASSES a correctly-wired repo. A gate
    # that only ever rejects is as broken as one that only ever accepts.
    print(f"      audit-wiring selftest OK — {len(CASES) + 1} cases, each accepted or rejected "
          f"as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
