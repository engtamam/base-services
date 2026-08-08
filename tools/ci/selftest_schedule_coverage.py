#!/usr/bin/env python
"""Prove safety:schedule-coverage can actually FAIL.

A gate nobody has watched fail is decoration — and this one guards a defect that shipped repeatedly
(a scheduled endpoint with no schedule). So build throwaway repos and assert the verdict: a covered
endpoint passes, an uncovered one fails, an orphan cron fails, and a reasoned exclusion passes.
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_schedule_coverage.py"

MANIFEST = '''ENDPOINTS = [
    {"path": "/internal/x/sweep", "tier": "internal", "method": "POST"},
    {"path": "/internal/x/notify", "tier": "internal", "method": "POST"},
]
'''

CRON_OK = 'cronJobs:\n  - name: sweep\n    schedule: "* * * * *"\n    path: /internal/x/sweep\n'
CRON_NONE = 'ingress:\n  enabled: false\n'
CRON_ORPHAN = (CRON_OK
               + '  - name: ghost\n    schedule: "* * * * *"\n    path: /internal/x/gone\n')


def _repo(tmp: pathlib.Path, *, values: str) -> None:
    app = tmp / "services" / "x-service" / "app"
    app.mkdir(parents=True)
    (tmp / "services" / "_template" / "app").mkdir(parents=True)
    (app / "manifest.py").write_text(MANIFEST, encoding="utf-8")
    vals = tmp / "deploy" / "services"
    vals.mkdir(parents=True)
    (vals / "x-service.values.yaml").write_text(values, encoding="utf-8")


def _run(tmp: pathlib.Path, exclude: str = "") -> tuple[int, str]:
    env = dict(os.environ, SCHEDULE_GATE_ROOT=str(tmp))
    if exclude:
        env["SCHEDULE_GATE_EXCLUDE"] = exclude
    else:
        env.pop("SCHEDULE_GATE_EXCLUDE", None)
    r = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


CASES = [
    # (name, values, exclude, must_fail, fragment)
    ("covered endpoint passes", CRON_OK, "", False, "schedule-coverage OK"),
    ("uncovered scheduler endpoint fails", CRON_NONE, "", True, "no CronJob"),
    ("orphan cron fails", CRON_ORPHAN, "", True, "not an internal POST endpoint"),
    # The escape hatch: an uncovered endpoint PASSES when excluded with a reason.
    ("reasoned exclusion passes", CRON_NONE, "x-service:/internal/x/sweep", False, "schedule-coverage OK"),
]


def main() -> int:
    failures = []
    for name, values, exclude, must_fail, fragment in CASES:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            _repo(tmp, values=values)
            rc, out = _run(tmp, exclude=exclude)
        failed = rc != 0
        if failed != must_fail:
            failures.append(f"  {name}: expected {'FAIL' if must_fail else 'PASS'}, "
                            f"got {'FAIL' if failed else 'PASS'}\n{out}")
        elif fragment not in out:
            failures.append(f"  {name}: right verdict, wrong reason — {fragment!r} not in:\n{out}")

    if failures:
        print("schedule-coverage selftest FAILED — the gate does not catch what it claims:")
        print("\n".join(failures))
        return 1
    print(f"      schedule-coverage selftest OK — {len(CASES)} cases, each accepted or rejected "
          f"as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
