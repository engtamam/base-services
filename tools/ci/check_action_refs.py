#!/usr/bin/env python
"""Gate: every `uses:` in every workflow resolves to a ref that actually exists.

A workflow may be perfectly valid YAML, pass every gate, and then die at `Set up job` with

    Unable to resolve action `aquasecurity/trivy-action@0.28.0`, unable to find version `0.28.0`

That is what happened to release v0.1.1. The gates went green, `discover services` went green,
and then all seventeen image jobs failed before a single step ran, because the action's tags are
`v`-prefixed and ours was not. Nothing was built. The tag had to be burned and re-cut.

The failure lands AFTER the gates report green, which is the same shape as every other defect
this repo has chased: the check passed, and the thing it was supposed to protect was broken.
A gate that runs before the build is the only place to catch it.

Rate limits and offline machines produce a SKIP, never a FAIL: an unreachable API says nothing
about whether the reference is right, and a gate that goes red for reasons unrelated to the code
teaches people to re-run it until it is green.

Exit codes: 0 ok · 1 a reference does not exist · 0 + notes when the API was unreachable
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import urllib.error
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"

# `uses: owner/repo@ref`, in both the bare and the list-item (`- uses:`) form. The first version
# of this regex omitted the `- ` and silently found 6 of 11 references — a checker that sees less
# than it claims to is the defect it was written to catch.
# Local (`./.github/...`) and docker (`docker://`) uses have no ref to resolve; they are skipped.
_USES = re.compile(r"^\s*-?\s*uses:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)@([A-Za-z0-9_.\-/]+)\s*$",
                   re.MULTILINE)

_TRANSIENT = (403, 429, 500, 502, 503, 504)


def _get(url: str) -> tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "base-services-gate"})
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:      # noqa: S310 - fixed https host
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""                                           # offline


def resolves(repo: str, ref: str) -> int:
    """200 = exists, 404 = does not, other = we could not tell."""
    for kind in ("tags", "heads"):
        status, _ = _get(f"https://api.github.com/repos/{repo}/git/ref/{kind}/{ref}")
        if status == 200:
            return 200
        if status in _TRANSIENT or status == 0:
            return status
    # a 40-hex ref is a commit sha; resolving it needs a different endpoint
    if re.fullmatch(r"[0-9a-f]{7,40}", ref):
        status, _ = _get(f"https://api.github.com/repos/{repo}/commits/{ref}")
        return status
    return 404


def main() -> int:
    if not WORKFLOWS.is_dir():
        print("  no .github/workflows/ — nothing to check")
        return 0

    refs: dict[tuple[str, str], list[str]] = {}
    for path in sorted(WORKFLOWS.glob("*.y*ml")):
        for repo, ref in _USES.findall(path.read_text(encoding="utf-8")):
            refs.setdefault((repo, ref), []).append(path.name)

    if not refs:
        print("  no external actions referenced")
        return 0

    bad: list[str] = []
    unknown: list[str] = []
    for (repo, ref), files in sorted(refs.items()):
        status = resolves(repo, ref)
        where = ", ".join(sorted(set(files)))
        if status == 200:
            print(f"  ok       {repo}@{ref}")
        elif status == 404:
            bad.append(f"{repo}@{ref}  (used by {where})")
        else:
            unknown.append(f"{repo}@{ref}  (HTTP {status or 'offline'})")

    for u in unknown:
        print(f"  note     could not verify {u}")

    if bad:
        print("\naction-refs: FAIL — these do not exist, and the job will die at `Set up job`:")
        for b in bad:
            print(f"    - {b}")
        print("  Every gate would pass first, and nothing would be built.")
        return 1

    print(f"\naction-refs: PASS — {len(refs) - len(unknown)} reference(s) resolve"
          + (f", {len(unknown)} unverified (API unreachable)" if unknown else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
