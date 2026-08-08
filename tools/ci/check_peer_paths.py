#!/usr/bin/env python
"""Gate: every peer-call PATH a service is configured with is served by SOME service.

The recurring seam defect — two components each correct alone, disagreeing where they meet — has a
config-shaped variant the contract-callers gate does not reach: a `*_PATH` DEFAULT in a service's
config that names a route no service serves. ticket-service shipped NOTIFICATION_SEND_PATH =
"/internal/notifications/send"; notification-service serves /internal/notify. So NOTIFY_DRIVER=http
sent every ticket notification to a silent 404 — a failed notification is not a failed ticket, so
nothing looked wrong until a customer said "you never replied" days later.

This reads the OTHER side of that seam. It collects every path served across all manifests, then
requires each `*_PATH` string default in every config.py (a path starting with `/`) to be one of
them, normalising `{id}`/`{tenant_id}`/`{}` placeholders on both sides. A path nobody serves is a
typo or drift, caught here instead of on the first complaint.

LIMIT, stated plainly: it checks STATIC `*_PATH` defaults. A fully dynamic peer target assembled at
runtime (a CSV of `service=url|undo` like ONBOARDING_PROVISIONERS) is not visible statically — the
boot-time check the field reports suggest (compare against the peer's /internal/manifest and refuse
to start) is the answer for those. This gate closes the common, static case with zero false
positives. Genuinely-external paths go in EXCLUDED with a reason.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("PEER_PATHS_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"

# `<NAME>_PATH: str = "/..."` — a configured peer-call path (or the target of a self-call).
_PATH_DEFAULT = re.compile(r'^\s+([A-Z][A-Z0-9_]*_PATH)\s*:\s*str\s*=\s*"(/[^"]*)"', re.MULTILINE)
_PARAM = re.compile(r"\{[^}]*\}")

# Config-path fields that legitimately name something NO base-service serves (an external system, a
# customer's own API) OR a KNOWN, reviewed gap. Keyed "<svc>:<FIELD>" -> reason. Empty today —
# ticket's ASSISTANT_TRIAGE/DRAFT paths are now served by assistant-service (internal tier).
EXCLUDED: dict[str, str] = {}
for _e in (os.environ.get("PEER_PATHS_GATE_EXCLUDE", "") or "").split(","):
    if _e.strip():
        EXCLUDED[_e.strip()] = "excluded via env (selftest)"


def _norm(path: str) -> str:
    return _PARAM.sub("{}", path.rstrip("/")) or "/"


def _served_paths() -> set[str]:
    """Every path declared in any service manifest (all tiers), normalised."""
    out: set[str] = set()
    for man in SERVICES.glob("*/app/manifest.py"):
        try:
            tree = ast.parse(man.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                d = {k.value: v.value for k, v in zip(node.keys, node.values)
                     if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
                if isinstance(d.get("path"), str) and "tier" in d:
                    out.add(_norm(d["path"]))
    return out


def _config_paths(svc: pathlib.Path) -> list[tuple[str, str]]:
    """(FIELD, path) for every `*_PATH` string default in this service's config.py."""
    cfg = svc / "app" / "config.py"
    if not cfg.is_file():
        return []
    return _PATH_DEFAULT.findall(cfg.read_text(encoding="utf-8"))


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    served = _served_paths()
    if not served:
        print("FATAL: no served paths found in any manifest — a gate with no subject cannot pass")
        return 1
    problems: list[str] = []
    checked = 0
    for svc in sorted(p for p in SERVICES.iterdir() if p.is_dir() and p.name != "_template"):
        for field, path in _config_paths(svc):
            checked += 1
            if f"{svc.name}:{field}" in EXCLUDED:
                continue
            if _norm(path) not in served:
                problems.append(
                    f"{svc.name}: {field} = {path!r} — no service serves that path. A peer call to it "
                    f"is a silent 404. Fix the path to one a peer's manifest declares, or list "
                    f"{svc.name}:{field} in EXCLUDED with a reason (external target).")
    if problems:
        print(f"safety:peer-paths — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nA configured peer path nobody serves is a 404 waiting for the first call — and a "
              "failed peer call is usually invisible from the caller's own side.")
        return 1
    print(f"safety:peer-paths OK — {checked} configured peer-path(s) across the platform, each served "
          f"by a real manifest endpoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
