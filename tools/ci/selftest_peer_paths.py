#!/usr/bin/env python
"""Prove safety:peer-paths can actually FAIL — it guards a defect that shipped (a config path nobody
serves). Build throwaway repos and assert the verdict: a served path passes, an unserved one fails,
a reasoned exclusion passes."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parents[2]
CHECK = ROOT / "tools" / "ci" / "check_peer_paths.py"

MANIFEST = '''ENDPOINTS = [
    {"path": "/internal/notify", "tier": "internal", "method": "POST"},
    {"path": "/internal/things/{id}", "tier": "internal", "method": "GET"},
]
'''


def _repo(tmp: pathlib.Path, path_default: str) -> None:
    # A "peer" that serves /internal/notify, and a "caller" whose config names a path.
    peer = tmp / "services" / "peer-service" / "app"
    peer.mkdir(parents=True)
    (peer / "manifest.py").write_text(MANIFEST, encoding="utf-8")
    caller = tmp / "services" / "caller-service" / "app"
    caller.mkdir(parents=True)
    (caller / "manifest.py").write_text("ENDPOINTS = []\n", encoding="utf-8")
    (caller / "config.py").write_text(
        f'class Settings:\n    NOTIFY_PATH: str = "{path_default}"\n', encoding="utf-8")
    (tmp / "services" / "_template" / "app").mkdir(parents=True)


def _run(tmp: pathlib.Path, exclude: str = "") -> tuple[int, str]:
    env = dict(os.environ, PEER_PATHS_GATE_ROOT=str(tmp))
    if exclude:
        env["PEER_PATHS_GATE_EXCLUDE"] = exclude
    else:
        env.pop("PEER_PATHS_GATE_EXCLUDE", None)
    r = subprocess.run([sys.executable, str(CHECK)], capture_output=True, text=True, env=env)
    return r.returncode, r.stdout + r.stderr


CASES = [
    # (name, path_default, exclude, must_fail, fragment)
    ("served path passes", "/internal/notify", "", False, "peer-paths OK"),
    ("param path matches with placeholder", "/internal/things/{ref}", "", False, "peer-paths OK"),
    ("unserved path fails", "/internal/notifications/send", "", True, "no service serves"),
    ("reasoned exclusion passes", "/internal/notifications/send",
     "caller-service:NOTIFY_PATH", False, "peer-paths OK"),
]


def main() -> int:
    failures = []
    for name, path_default, exclude, must_fail, fragment in CASES:
        with tempfile.TemporaryDirectory() as td:
            tmp = pathlib.Path(td)
            _repo(tmp, path_default)
            rc, out = _run(tmp, exclude=exclude)
        failed = rc != 0
        if failed != must_fail:
            failures.append(f"  {name}: expected {'FAIL' if must_fail else 'PASS'}, "
                            f"got {'FAIL' if failed else 'PASS'}\n{out}")
        elif fragment not in out:
            failures.append(f"  {name}: right verdict, wrong reason — {fragment!r} not in:\n{out}")
    if failures:
        print("peer-paths selftest FAILED:")
        print("\n".join(failures))
        return 1
    print(f"      peer-paths selftest OK — {len(CASES)} cases, each accepted or rejected as intended")
    return 0


if __name__ == "__main__":
    sys.exit(main())
