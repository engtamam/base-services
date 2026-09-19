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
import re
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
    # audit-service now defaults to the HMAC-keyed integrity chain (fails closed without a key), so a
    # zero-infra boot needs one. Harmless to services that do not read it.
    "INTEGRITY_HMAC_KEY": "openapi-boot-integrity-key-not-a-real-one",
}

# ── routes that only exist when a feature is switched on ──────────────────────
#
# THE SPEC USED TO DESCRIBE ONE DEPLOYMENT AND CLAIM TO DESCRIBE THE SERVICE.
#
# Some routers are mounted conditionally — identity-service mounts its tenancy and members routers
# only when IDENTITY_MULTI_TENANT is set, so that a deployment with the feature off 404s them
# rather than exposing a feature it has no store for. That is the right runtime behaviour. But the
# spec was exported with default settings, so those six endpoints appeared in no document at all:
# an operator who switched multi-tenancy on got routes the published spec said did not exist, and
# any gate or client generator reading that file concluded a live route was unserved.
#
# It is the drift this tool was written to stop, pointing the other way. A spec that promises what
# the code lacks fails loudly the first time somebody calls it. A spec that OMITS what the code
# serves fails only when somebody believes it — which is later, and quieter.
#
# So the spec is now the UNION of what a service can serve, with every conditional path marked
# `x-available-when` naming the setting that turns it on. One honest document, rather than one
# document per deployment or a document that silently picks a deployment.
FEATURE_FLAGS: dict[str, dict[str, str]] = {
    # IDENTITY_MULTI_TENANT=true exposes the tenancy/members routers; but with it on, identity's boot
    # guard refuses an empty PLATFORM_TENANT (the platform-capability boundary would be undecidable),
    # so a spec export must name one — any value satisfies the guard and reaches no network.
    "identity-service": {"IDENTITY_MULTI_TENANT": "true", "SERVICE_ACCOUNTS_ENABLED": "true",
                         "PLATFORM_TENANT": "platform"},
}

# Any `if settings.X:` guarding an include_router. Used to FAIL when a service grows a conditional
# mount whose flag nobody added above — otherwise the next one silently drops its routes from the
# spec exactly as this one did, and the omission is invisible in review.
_CONDITIONAL_MOUNT = re.compile(
    r"if\s+(?:settings|s|self\._?cfg)\.([A-Z_][A-Z0-9_]*)\s*:\s*\n"
    r"(?:\s*#[^\n]*\n)*"
    r"(?:\s*app\.include_router)", re.M)

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


def _export(py: pathlib.Path, service: pathlib.Path, env: dict[str, str]) -> dict | None:
    r = subprocess.run([str(py), "-c", _EXPORT % env], cwd=service,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0 or "<<<SPEC>>>" not in r.stdout:
        print(f"      {service.name}: could not export ({r.stderr.strip().splitlines()[-1:] or ''})")
        return None
    # The app logs to stdout on import, so take only what follows the marker.
    return json.loads(r.stdout.split("<<<SPEC>>>", 1)[1].strip())


def undeclared_flags(service: pathlib.Path) -> list[str]:
    """Settings that gate an include_router but are not declared in FEATURE_FLAGS."""
    main = service / "app" / "main.py"
    if not main.exists():
        return []
    known = set(FEATURE_FLAGS.get(service.name, {}))
    return sorted(set(_CONDITIONAL_MOUNT.findall(main.read_text(encoding="utf-8"))) - known)


def spec_for(service: pathlib.Path) -> str | None:
    """The service's OpenAPI document: everything it can serve, with the conditional parts marked.

    Exported TWICE when the service has feature-gated routers — once with the features off, once
    with them on. The difference is exactly the set of conditional paths, which is more reliable
    than mapping routers to paths by hand and cannot drift as routes move between routers.
    """
    py = python_for(service)
    if py is None:
        return None

    flags = FEATURE_FLAGS.get(service.name, {})
    baseline = _export(py, service, ENV)
    if baseline is None:
        return None
    if not flags:
        return json.dumps(baseline, indent=2, sort_keys=True) + "\n"

    full = _export(py, service, {**ENV, **flags})
    if full is None:
        return None

    conditional = set(full.get("paths", {})) - set(baseline.get("paths", {}))
    note = ", ".join(f"{k}={v}" for k, v in sorted(flags.items()))
    for path in conditional:
        # An adopter reading this needs the SETTING, not merely a warning that the path is
        # optional: "exists when IDENTITY_MULTI_TENANT=true" is actionable, "may not exist" is not.
        full["paths"][path]["x-available-when"] = note
    return json.dumps(full, indent=2, sort_keys=True) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="regenerate the committed specs")
    ap.add_argument("--check", action="store_true", help="fail if a committed spec is stale")
    args = ap.parse_args()
    if not (args.write or args.check):
        ap.error("pass --write or --check")

    OUT.mkdir(parents=True, exist_ok=True)
    stale, written, skipped = [], 0, []

    # A conditional mount whose flag is not declared exports a spec MISSING those routes, and
    # nothing about the output says so. Refuse before writing anything: publishing a quietly
    # incomplete document is the failure being fixed, and doing it while reporting success is how
    # it went unnoticed for as long as it did.
    undeclared = {s.name: flags for s in sorted(p for p in SERVICES.iterdir() if p.is_dir())
                  if (flags := undeclared_flags(s))}
    if undeclared:
        print("      a router is mounted behind a setting nobody declared, so its routes would")
        print("      be missing from the published spec with no sign they exist:")
        for name, flags in undeclared.items():
            print(f"        {name}: {', '.join(flags)}")
        print("      Fix: add the setting to FEATURE_FLAGS in tools/ci/gen_openapi.py with the")
        print("      value that switches the feature ON.")
        return 1

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
