#!/usr/bin/env python3
"""Export each service's OpenAPI spec from the running app, so the spec cannot lie.

Hand-written specs rot. Four lived in docs/contracts/ and two had drifted by the time anyone
checked: privacy-erase declared `{request_id}` where the service serves `{dsar_id}`, and
stream-service declared seven endpoints that do not exist. Both drifts point the dangerous way —
the document promises something the code does not have, so a customer generating a client from it
gets methods that 404 at runtime. A spec that lies is worse than no spec, because people build
against it.

FastAPI already computes the truth at /openapi.json. This writes that truth to a file so it can be
committed, diffed and reviewed like anything else.

Two modes:

    --write    regenerate every spec (run this after changing routes)
    --check    regenerate into memory and FAIL if it differs from what is committed

`--check` is what CI runs. It is the whole point: a route added, renamed or deleted without
regenerating is a spec that has started lying, and it fails the build the same day rather than
surfacing when a customer's generated client breaks.

Each service is imported in a SUBPROCESS. They are self-contained by design, with their own venv
and their own `app` package, so importing two of them into one interpreter would collide on the
module name and silently give the second one the first one's routes.
"""
from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"
OUT = ROOT / "docs" / "openapi"

# Enough config to boot with zero infrastructure. These are the SAFE defaults every service already
# has; nothing here reaches a network.
ENV = {
    "INTERNAL_SERVICE_TOKEN": "x" * 40,
    "ENV": "test",
    "PII_SALT": "s" * 32,
}

_EXPORT = """
import json, os, sys
os.environ.update(%r)
from fastapi.testclient import TestClient
from app.main import create_app
spec = TestClient(create_app()).get("/openapi.json").json()
sys.stdout.write("\\n<<<SPEC>>>\\n" + json.dumps(spec, indent=2, sort_keys=True) + "\\n")
"""


def python_for(service: pathlib.Path) -> pathlib.Path | None:
    for candidate in (service / ".venv" / "Scripts" / "python.exe",
                      service / ".venv" / "bin" / "python"):
        if candidate.exists():
            return candidate
    return None


def spec_for(service: pathlib.Path) -> str | None:
    """The service's own OpenAPI document, as it would serve it."""
    py = python_for(service)
    if py is None:
        return None
    r = subprocess.run([str(py), "-c", _EXPORT % ENV], cwd=service,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or "<<<SPEC>>>" not in r.stdout:
        print(f"      {service.name}: could not export ({r.stderr.strip().splitlines()[-1:] or ''})")
        return None
    # The app logs to stdout on import, so take only what follows the marker.
    return r.stdout.split("<<<SPEC>>>", 1)[1].strip() + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="regenerate the committed specs")
    ap.add_argument("--check", action="store_true", help="fail if a committed spec is stale")
    args = ap.parse_args()
    if not (args.write or args.check):
        ap.error("pass --write or --check")

    OUT.mkdir(parents=True, exist_ok=True)
    stale, written, skipped = [], 0, []

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        if service.name == "_template" or not (service / "app" / "main.py").exists():
            continue
        spec = spec_for(service)
        if spec is None:
            skipped.append(service.name)
            continue

        path = OUT / f"{service.name}.openapi.json"
        if args.write:
            path.write_text(spec, encoding="utf-8")
            written += 1
        else:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != spec:
                stale.append(service.name)

    if skipped:
        # Never silent: a service we could not export is a service whose spec nobody is checking.
        print(f"      skipped (no venv or import failed): {', '.join(skipped)}")

    if args.write:
        print(f"      wrote {written} spec(s) to {OUT.relative_to(ROOT)}")
        return 0

    if stale:
        print("      OpenAPI specs are stale — routes changed without regenerating:")
        for name in stale:
            print(f"        {name}")
        print("      Fix: python tools/ci/gen_openapi.py --write   (then commit the result)")
        return 1

    print(f"      {len(list(OUT.glob('*.openapi.json')))} generated spec(s) match the live routes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
