#!/usr/bin/env python
"""Gate: services/_template ships GREEN — its own pytest suite passes.

`create_service.py` copies `_template`, then SELF-VERIFIES the new service by running pytest + the
security gate, and only commits on green. That promise is only worth anything if the seed is green
to begin with: a red `_template` makes every generation roll back, and the only way to create a
service becomes `--no-verify` — which turns a self-verifying generator into a copier and drops the
guarantee entirely.

Nothing else runs `_template`'s suite. The `suites` gate and every coverage gate enumerate services
through `all_services()`, which deliberately EXCLUDES `_template` (it is not deployed, not scanned
for release). So the seed can rot red for months while all 23 real services stay green — which is
exactly how it drifted: a fleet-wide adapter (audit_sink/http.py) changed its retry contract, the
copied tests were not all updated, and `_template`'s went red unnoticed. This gate is the one place
that looks.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(os.environ.get("TEMPLATE_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
TEMPLATE = ROOT / "services" / "_template"


def _python() -> str:
    """The _template venv interpreter (it carries pytest + httpx), else this one."""
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        cand = TEMPLATE / rel
        if cand.is_file():
            return str(cand)
    return sys.executable


def main() -> int:
    if not TEMPLATE.is_dir():
        print(f"FATAL: no services/_template under {ROOT}")
        return 1
    py = _python()
    # PYTHONHASHSEED pinned so a dict-ordering bug in the seed cannot hide, matching the suites gate.
    env = dict(os.environ, PYTHONHASHSEED="0", DATASTORE_TEST_DSN="")
    proc = subprocess.run([py, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                          cwd=str(TEMPLATE), env=env, capture_output=True, text=True)
    tail = "\n".join((proc.stdout or "").strip().splitlines()[-3:])
    if proc.returncode != 0:
        print("safety:template-green — services/_template is RED; create_service.py would roll back:")
        print("\n".join(f"  {ln}" for ln in (proc.stdout or "").splitlines()[-25:]))
        if proc.stderr.strip():
            print("  stderr:", proc.stderr.strip().splitlines()[-1])
        return 1
    print(f"safety:template-green OK — the seed passes ({tail or 'green'})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
