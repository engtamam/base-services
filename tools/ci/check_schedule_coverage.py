#!/usr/bin/env python
"""Gate: every scheduler-driven internal endpoint has a CronJob in its values — and vice versa.

A scheduled endpoint and its schedule are not independently useful, yet they live in two files: the
handler in `app/api/internal.py` (declared in `app/manifest.py`) and the CronJob in
`deploy/services/<svc>.values.yaml`. Ship the endpoint without the schedule and it has NO caller —
`queued` becomes a prettier word for `lost`, a retention policy is stored and never runs, closed
tenants are never erased — and every other check passes, because the endpoint works when POSTed by
hand. This has shipped THREE+ times (audit retention, job scheduler-tick, notification drain), and a
sweep of the manifests found four more with no cron at all (billing grants-drain, compliance
consent sweep-lapses, tenant close-sweep, ticket notifications-drain).

So this gate ties the two together. For each service it reads the internal POST endpoints from the
manifest and the CronJob `path`s from the values file, and requires:

  * COVERAGE — every internal POST endpoint whose last path segment looks like a scheduler verb
    (sweep, drain, tick, prune, reclaim, drill, retention, scan, cycle-due) has a CronJob targeting
    it, UNLESS it is listed in EXCLUDED with a reason (a sweep triggered by a peer, not a clock).
  * NO ORPHANS — every CronJob `path` is a real internal endpoint of that service, so a typo or a
    renamed endpoint fails here rather than firing a schedule at a 404 forever.

It is a heuristic net, not a proof: an endpoint that needs a schedule but is named without a
scheduler verb (a plain `reconcile`, say) will not be caught. That is the deliberate limit — the
verb set catches the common shapes with near-zero false positives; widen it, or declare the
endpoint in EXCLUDED, as real cases appear.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(os.environ.get("SCHEDULE_GATE_ROOT", "")
                    or pathlib.Path(__file__).resolve().parents[2])
SERVICES = ROOT / "services"
VALUES = ROOT / "deploy" / "services"

# Last-path-segment substrings that mean "a clock drives this", not "a peer calls this".
SCHEDULER_VERBS = ("sweep", "drain", "tick", "prune", "reclaim", "drill", "retention", "scan",
                   "cycle-due")

# Scheduler-shaped endpoints that are DELIBERATELY not cron-driven — {"<svc>:<path>": "why"}. The
# escape hatch exists so a false positive is a reviewed decision, not an edit to the verb list that
# weakens the net for everyone. The selftest augments it via env.
EXCLUDED: dict[str, str] = {
    # NOT a sweep: this reclaims ONE grant, addressed by a required grant_id in the request body — a
    # per-grant primitive a customer action or a future 'reclaim idle balance' flow calls, not a
    # clock. A CronJob has no grant_id to pass. The genuinely scheduled wallet sweeps are
    # sweep-grants / notice-grants (which do carry CronJobs); this shares the 'reclaim' verb but not
    # the shape.
    "wallet-service:/internal/wallet/reclaim": "per-grant primitive (requires grant_id in the body), "
                                               "not a scheduler sweep — sweep-grants/notice-grants are",
}
for _e in (os.environ.get("SCHEDULE_GATE_EXCLUDE", "") or "").split(","):
    if _e.strip():
        EXCLUDED[_e.strip()] = "excluded via env (selftest)"

# CronJobs write `path: /internal/...`; ingress writes `paths:` (plural, list items). So a line
# `<indent>path: <value>` is unambiguously a CronJob target across the generated values file.
_CRON_PATH = re.compile(r"^\s+path:\s*(\S+)\s*$", re.MULTILINE)


def _internal_posts(manifest: pathlib.Path) -> set[str]:
    """Every {tier: internal, method: POST} endpoint path declared in a manifest (literal read)."""
    out: set[str] = set()
    if not manifest.is_file():
        return out
    try:
        tree = ast.parse(manifest.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return out
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            d = {k.value: v.value for k, v in zip(node.keys, node.values)
                 if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)}
            if d.get("tier") == "internal" and d.get("method") == "POST" and isinstance(d.get("path"), str):
                out.add(d["path"])
    return out


def _cron_paths(values: pathlib.Path) -> list[str]:
    if not values.is_file():
        return []
    return _CRON_PATH.findall(values.read_text(encoding="utf-8"))


def _looks_scheduled(path: str) -> bool:
    # Match a verb as a whole hyphen-delimited WORD in the last segment, not a bare substring: a real
    # scheduler endpoint is `sweep-lapses` / `grants-drain` / `cycle-due` (the verb is one of the
    # hyphen parts, or the whole segment), whereas `tickets` merely CONTAINS "tick" and is a
    # ticket-list read. Substring matching flagged `/internal/tickets` as a scheduler tick — a false
    # positive that a word match cleans up without weakening the net for the hyphenated cases.
    seg = path.rstrip("/").rsplit("/", 1)[-1]
    parts = seg.split("-")
    return any(verb == seg or verb in parts for verb in SCHEDULER_VERBS)


def check_service(svc: pathlib.Path, problems: list[str]) -> int:
    posts = _internal_posts(svc / "app" / "manifest.py")
    crons = _cron_paths(VALUES / f"{svc.name}.values.yaml")
    cron_set = set(crons)
    covered = 0

    # COVERAGE: a scheduler-shaped endpoint with no cron.
    for path in sorted(posts):
        if not _looks_scheduled(path):
            continue
        covered += 1
        if path in cron_set or f"{svc.name}:{path}" in EXCLUDED:
            continue
        problems.append(
            f"{svc.name}: {path} looks scheduler-driven but has no CronJob in "
            f"deploy/services/{svc.name}.values.yaml — add it to CRONS in "
            f"scripts/gen_deploy_values.py and regenerate, or list it in EXCLUDED with a reason")

    # NO ORPHANS: a cron pointing at a path this service does not serve.
    internal_all = _internal_posts(svc / "app" / "manifest.py")
    for path in crons:
        if path not in internal_all:
            problems.append(
                f"{svc.name}: a CronJob targets {path}, which is not an internal POST endpoint this "
                f"service declares — a firing schedule aimed at a 404. Fix the path or the manifest")
    return covered


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT}")
        return 1
    problems: list[str] = []
    checked = covered = 0
    for svc in sorted(SERVICES.iterdir()):
        if not svc.is_dir() or svc.name == "_template":
            continue
        if not (svc / "app" / "manifest.py").is_file():
            continue
        checked += 1
        covered += check_service(svc, problems)
    if not checked:
        print("FATAL: no services with a manifest — a gate with no subject cannot pass")
        return 1
    if problems:
        print(f"safety:schedule-coverage — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nA scheduled endpoint without its schedule has no caller: the work never runs and "
              "every other check stays green. Ship the CronJob with the endpoint.")
        return 1
    print(f"safety:schedule-coverage OK — {covered} scheduler-driven endpoint(s) across {checked} "
          f"service(s), each with a CronJob (or excluded with a reason); no orphaned schedules")
    return 0


if __name__ == "__main__":
    sys.exit(main())
