#!/usr/bin/env python
"""Emit the matrix of services that a change actually affects.

CI reruns all 17 services on every push, even a one-line edit to one of them. This narrows the
matrix to what changed — but the whole risk of "build only what changed" is building TOO LITTLE,
so the rules are deliberately conservative:

  * A change confined to `services/<x>/` selects only `<x>`.
  * A change to ANYTHING SHARED — the chart, `tools/`, `.github/`, the root, or the `_template`
    seed every service is cloned from — selects EVERY service. Shared code changes everyone.
  * When the base cannot be determined, the diff is empty for a susp_reason, or anything looks
    off, the answer is EVERY service. The default is never "skip".

The output is the same JSON array the discover job already produces, so the matrix consumes it
unchanged. Prints to stdout; a one-line reason goes to stderr.

    python tools/ci/changed_services.py --base origin/main
    python tools/ci/changed_services.py --base "$GITHUB_BASE_REF" --all-if-empty
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# A change touching any of these forces the FULL set — they are shared by every service, or they
# change how services are built, tested, or discovered.
SHARED_PREFIXES = (
    "deploy/",          # the chart every service renders through
    "tools/",           # the gate runner, the scripts every gate calls
    ".github/",         # the workflows themselves
    "services/_template/",   # the seed every service is a copy of
)
# Root-level files that affect everything (anything not under a subdir we recognise).
SHARED_ROOT_SUFFIXES = ("pyproject.toml", ".gitattributes", ".dockerignore")


def _git(*args: str) -> str | None:
    try:
        r = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


def all_services() -> list[str]:
    return sorted(p.name for p in SERVICES.iterdir()
                  if p.is_dir() and p.name != "_template" and (p / "pyproject.toml").is_file())


def changed_files(base: str) -> list[str] | None:
    """Files changed between `base` and HEAD. None if the range cannot be resolved."""
    mb = _git("merge-base", base, "HEAD")
    ref = mb.strip() if mb else base
    out = _git("diff", "--name-only", f"{ref}...HEAD")
    if out is None:
        out = _git("diff", "--name-only", ref)      # base is a bare commit, not a branch
    if out is None:
        return None
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


def forces_full(files: list[str]) -> str | None:
    for f in files:
        if any(f.startswith(p) for p in SHARED_PREFIXES):
            return f
        if "/" not in f and f.endswith(SHARED_ROOT_SUFFIXES):
            return f
        if "/" not in f:                             # any other root file (README, LICENSE, …)
            return f
    return None


def selected(base: str, everything: list[str]) -> tuple[list[str], str]:
    files = changed_files(base)
    if files is None:
        return everything, f"could not diff against {base!r} — building all (fail safe)"
    if not files:
        return everything, "no changed files resolved — building all (fail safe)"

    trigger = forces_full(files)
    if trigger:
        return everything, f"shared path changed ({trigger}) — building all"

    hit = {f.split("/")[1] for f in files
           if f.startswith("services/") and len(f.split("/")) > 2}
    chosen = [s for s in everything if s in hit]
    if not chosen:
        # changed files were all under services/ but matched no known service (a deleted one?).
        return everything, "changed services did not resolve — building all (fail safe)"
    return chosen, f"{len(chosen)} of {len(everything)} services changed"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="", help="ref to diff against (branch or commit)")
    ap.add_argument("--all-if-empty", action="store_true",
                    help="with no --base, emit every service instead of failing")
    args = ap.parse_args()

    everything = all_services()
    if not everything:
        print("[]")
        print("FATAL: no services discovered", file=sys.stderr)
        return 1

    if not args.base:
        if args.all_if_empty:
            print(json.dumps(everything))
            print("no base given — building all", file=sys.stderr)
            return 0
        print("[]")
        print("FATAL: --base is required (or pass --all-if-empty)", file=sys.stderr)
        return 1

    chosen, reason = selected(args.base, everything)
    print(json.dumps(sorted(chosen)))
    print(reason, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
