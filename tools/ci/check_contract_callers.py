#!/usr/bin/env python3
"""safety:contract-callers — a cross-service contract that nobody calls is not a control.

This is the fourth appearance of one defect: code that is complete, correct, and connected to
nothing.

    audit-service        a hash-chained log with a working ingest endpoint and, for months, no
                         writers. The chain verified perfectly and held zero events.
    compliance-service   consent-check: default-deny, cached, correct — and ZERO callers across
                         nineteen services for four phases. Not even an unwired config knob.
    stream-service       ExportStore, a complete create/get/save with no creation path.
    onboarding-service   an anti-abuse registry with no writer.

safety:dead-code catches the third and misses the first two, and the reason is structural rather
than a tuning problem: it finds a SYMBOL nobody uses. A server endpoint with no clients has no
unused symbols — its handler is referenced by its own router, its store by its own service. Every
half is individually correct. The absence lives in the space BETWEEN two services, which is
exactly where no unit test looks.

So this gate reads both sides of the seam:

  R1  A contract marked `"caller": "service"` that no service declares consuming.
      The original defect, stated directly.

  R2  A CONSUMES entry naming a path nobody serves.
      Drift. The producer renamed or removed it; we still believe we call it.

  R3  A CONSUMES entry whose declared port does not exist.
      A declaration with no code behind it, which would rebuild the same lie one level up — and
      this rule is the one that keeps the gate honest, because a gate satisfied by declarations
      alone is satisfied by fiction.

WHAT IS DELIBERATELY NOT CHECKED, and why saying so matters: an internal endpoint with no
`caller` key is OUT OF SCOPE, not implicitly fine. Most of the 107 internal endpoints are CronJob
targets, and inferring "this one is a sweeper" from its name would be guesswork dressed as a rule.
Marking them is incremental work; claiming coverage we do not have would make this gate the thing
it exists to prevent.
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# Contracts served for a caller that is NOT another service in this repo, with the reason. An
# entry here is a claim someone has to defend, which is the point of writing it down.
ALLOWLIST: dict[str, str] = {
    "/internal/compliance/hold-check":
        "Serves the PER-SUBJECT shape, for callers that purge one subject at a time and hold a "
        "raw identifier — which includes the external team this was built for. Our own services "
        "consume the bulk held-refs shape instead, because audit-service purges by scanning and "
        "holds only pseudonyms. Two shapes of the same question, each with a caller; this one's "
        "caller is outside this repo, which is what an allowlist entry is for.",
}


def served(service: pathlib.Path) -> list[dict]:
    """Internal endpoints from a service's manifest, read as TEXT.

    The manifest is a function that builds a dict, so it cannot be literal_eval'd whole — but the
    endpoint entries are literal dicts, and matching them is enough. The gate must never import a
    service to inspect it: importing runs module-level code, and a gate that executes what it
    audits is not a gate.
    """
    mf = service / "app" / "manifest.py"
    if not mf.exists():
        return []
    out: list[dict] = []
    text = mf.read_text(encoding="utf-8")
    for match in re.finditer(r"\{\"path\":\s*\"(/internal/[^\"]+)\"[^}]*\}", text, re.S):
        entry = match.group(0)
        caller = re.search(r"\"caller\":\s*\"(\w+)\"", entry)
        out.append({"path": match.group(1), "caller": caller.group(1) if caller else ""})
    return out


def consumes(service: pathlib.Path) -> list[dict]:
    """The service's CONSUMES declaration, or [] if it declares none."""
    decl = service / "app" / "service_contracts.py"
    if not decl.exists():
        return []
    tree = ast.parse(decl.read_text(encoding="utf-8"))
    for node in tree.body:
        target = getattr(node, "target", None) or (getattr(node, "targets", [None]) or [None])[0]
        if getattr(target, "id", "") == "CONSUMES":
            try:
                return ast.literal_eval(node.value)
            except (ValueError, SyntaxError) as exc:
                raise ValueError(
                    f"{service.name}: CONSUMES is not a pure literal ({exc}). No variables or "
                    f"function calls — the gate reads it without importing your service.") from exc
    return []


def main() -> int:
    services = sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template")

    all_served: dict[str, str] = {}          # path -> serving service
    needs_caller: dict[str, str] = {}        # path -> serving service
    for svc in services:
        for entry in served(svc):
            all_served[entry["path"]] = svc.name
            if entry["caller"] == "service":
                needs_caller[entry["path"]] = svc.name

    declared: dict[str, list[str]] = {}      # path -> consuming services
    problems: list[str] = []

    for svc in services:
        for entry in consumes(svc):
            path, port = entry.get("path", ""), entry.get("port", "")
            declared.setdefault(path, []).append(svc.name)

            # R2 — drift.
            if path not in all_served:
                problems.append(
                    f"  {svc.name}: declares it calls {path!r}, which NO service serves.\n"
                    f"      Either the producer renamed it and this call is already broken, or "
                    f"the path is a typo. Both fail at runtime, silently.")
            elif entry.get("service") and entry["service"] != all_served[path]:
                problems.append(
                    f"  {svc.name}: says {entry['service']} serves {path!r}, but "
                    f"{all_served[path]} does.")

            # R3 — a declaration with no code behind it.
            if port and not (svc / "app" / "ports" / f"{port}.py").exists():
                problems.append(
                    f"  {svc.name}: declares it calls {path!r} through port {port!r}, but\n"
                    f"      app/ports/{port}.py does not exist. A declaration with no adapter is\n"
                    f"      a claim, not a call — the same defect this gate exists to catch.")

    # R1 — the original defect.
    for path, producer in sorted(needs_caller.items()):
        if path in declared:
            continue
        if path in ALLOWLIST:
            continue
        problems.append(
            f"  {producer}: serves {path!r} for a SERVICE caller and no service declares\n"
            f"      calling it. Either wire a consumer and declare it in\n"
            f"      app/service_contracts.py, or allowlist it here with the reason its caller\n"
            f"      is not a service in this repo.")

    if problems:
        print("      a cross-service contract is not connected:")
        print("\n".join(problems))
        return 1

    checked = len(needs_caller)
    links = sum(len(v) for v in declared.values())
    print(f"      {checked} service-called contract(s) have callers; "
          f"{links} declared call(s) resolve to a real endpoint and a real port")
    return 0


if __name__ == "__main__":
    sys.exit(main())
