#!/usr/bin/env python3
# =============================================================================
# feature-flag-service — License gate (permissive-only, stdlib)
# -----------------------------------------------------------------------------
# Enforces the project rule: dependencies must be PERMISSIVE. Strong/network
# copyleft and source-available/non-commercial licenses FAIL the build. Weak
# copyleft (MPL/LGPL) PASSES with a warning (safe for unmodified use). An
# UNKNOWN license FAILS — forcing every new dependency to be vetted.
#
# Verdicts: ALLOW (permissive) · WARN (weak copyleft, ok) · FORBID · UNKNOWN
# Exit code: 0 if no FORBID/UNKNOWN, else 1.
# =============================================================================
from __future__ import annotations

import sys
import os
import pathlib
import re

from importlib import metadata

ALLOW, WARN, FORBID, UNKNOWN = "ALLOW", "WARN", "FORBID", "UNKNOWN"

# Curated overrides for packages whose metadata is missing/ambiguous (name -> verdict).
OVERRIDES: dict[str, str] = {
    "cryptography": ALLOW,       # Apache-2.0 OR BSD-3-Clause
    "typing-extensions": ALLOW,  # PSF
    "pip": ALLOW,                # MIT
    "setuptools": ALLOW,         # MIT
    "wheel": ALLOW,              # MIT
}


def classify_one(text: str) -> str:
    u = text.upper()
    # ── FORBID: network/strong copyleft, source-available, non-commercial ──
    if "AGPL" in u or "AFFERO" in u:
        return FORBID
    if "SSPL" in u or "SERVER SIDE PUBLIC" in u:
        return FORBID
    for tok in (
        "BUSL", "BUSINESS SOURCE", "ELASTIC LICENSE", "COMMONS CLAUSE",
        "NON-COMMERCIAL", "NONCOMMERCIAL", "CC-BY-NC", "PROSPERITY",
        "EUPL", "CDDL", "ECLIPSE PUBLIC", "EPL-",
    ):
        if tok in u:
            return FORBID
    # ── WARN: weak copyleft (ok as unmodified dependency) ──
    if "LGPL" in u or "LESSER GENERAL PUBLIC" in u:
        return WARN
    if "MPL" in u or "MOZILLA PUBLIC" in u:
        return WARN
    # ── FORBID: strong copyleft (after LGPL handled) ──
    if "GPL" in u or "GENERAL PUBLIC LICENSE" in u:
        return FORBID
    # ── ALLOW: permissive ──
    for tok in (
        "MIT", "BSD", "APACHE", "ISC", "PYTHON SOFTWARE FOUNDATION", "PSF",
        "0BSD", "UNLICENSE", "ZLIB", "HISTORICAL PERMISSION", "HPND",
        "PYTHON-2.0", "POSTGRESQL",
    ):
        if tok in u:
            return ALLOW
    return UNKNOWN


def _candidates(dist: metadata.Distribution) -> list[str]:
    out: list[str] = []
    md = dist.metadata
    for c in md.get_all("Classifier", []):
        if "License" in c:
            out.append(c)
    expr = md.get("License-Expression")
    if expr:
        out.append(expr)
    lic = md.get("License")
    # A short License field is a name; a long one is full text (misleading) — skip long.
    if lic and len(lic) < 60:
        out.append(lic)
    return out


def classify_distribution(name: str, candidates: list[str]) -> tuple[str, str]:
    if name.lower() in OVERRIDES:
        return OVERRIDES[name.lower()], "override"
    verdicts = [classify_one(c) for c in candidates]
    detail = "; ".join(candidates)[:80] if candidates else "(no license metadata)"
    if FORBID in verdicts:
        return FORBID, detail
    if ALLOW in verdicts:
        return ALLOW, detail
    if WARN in verdicts:
        return WARN, detail
    return UNKNOWN, detail


# OUR OWN package. It is the PRODUCT, not a dependency of itself.
#
# This checker enforces one rule: every DEPENDENCY must be permissive, because a copyleft one can
# force us to publish our source. The opposite rule governs the product — it is proprietary, and
# /LICENSE grants nothing (safety:own-license enforces that).
#
# The two rules point in opposite directions, and the day the product stopped declaring itself
# Apache-2.0, this checker found `LicenseRef-Proprietary` in the installed distributions and
# reported the product as a forbidden dependency. It was right about the licence and wrong about
# what it was looking at. A service is not its own supplier.


def _own_package_name() -> str:
    """The `name = ` in this service's pyproject — derived, never typed twice."""
    pp = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    if pp.is_file():
        m = re.search(r'^\s*name\s*=\s*"([^"]+)"', pp.read_text(encoding="utf-8"),
                      re.MULTILINE)
        if m:
            return m.group(1).strip().lower()
    return ""


_OWN = os.environ.get("OWN_PACKAGE", "") or _own_package_name()


def _canon(name: str) -> str:
    """PEP 503 canonical form so 'PyJWT', 'pyjwt' and 'py_jwt' compare equal."""
    return re.sub(r"[-_.]+", "-", (name or "").strip()).lower()


def _req_name(req: str) -> str:
    """The distribution name out of a requirement/Requires-Dist string.

    'uvicorn[standard]>=0.30,<1.0' -> 'uvicorn'; 'anyio (>=3.4); extra == "x"' -> 'anyio'."""
    head = re.split(r"[\s<>=!~;(\[]", (req or "").strip(), maxsplit=1)[0]
    return _canon(head)


def _declared() -> set[str]:
    """Every top-level requirement this service DECLARES — runtime deps and every extra/dev/ci
    group in its pyproject. This is the service's own supply chain; the license rule covers dev
    tooling too, so the groups are included, but nothing that is merely present in the interpreter."""
    pp = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    if not pp.is_file():
        return set()
    try:
        import tomllib
        data = tomllib.loads(pp.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — an unparseable pyproject falls back to scanning all
        return set()
    proj = data.get("project", {}) or {}
    reqs = list(proj.get("dependencies", []) or [])
    for group in (proj.get("optional-dependencies", {}) or {}).values():
        reqs.extend(group or [])
    for group in (data.get("dependency-groups", {}) or {}).values():   # PEP 735, if used
        reqs.extend(x for x in (group or []) if isinstance(x, str))
    return {n for n in (_req_name(r) for r in reqs) if n}


def _closure() -> set[str]:
    """Declared deps plus their transitive Requires-Dist, canonicalised.

    The gate must judge what this service SHIPS, not whatever else lives in the developer's
    interpreter. In the CI container the two coincide; on a working machine they do not, and a gate
    that fails on unrelated locally-installed packages (another project's AGPL dep, say) is one
    people learn to bypass with --no-verify. Resolving the closure from pyproject makes the verdict
    the same everywhere. An empty closure (no or unparseable pyproject) signals 'scan everything',
    so the gate never silently checks nothing."""
    roots = _declared()
    if not roots:
        return set()
    seen: set[str] = set()
    queue = list(roots)
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue                     # a declared-but-not-installed extra: nothing to descend
        for req in (dist.requires or []):
            dep = _req_name(req)
            if dep and dep not in seen:
                queue.append(dep)
    return seen


def scan() -> list[tuple[str, str, str, str]]:
    closure = _closure()
    results: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        cn = _canon(name)
        if cn in seen:
            continue
        if _OWN and cn == _canon(_OWN):
            continue                     # the product, not a dependency
        # With a declared closure, judge ONLY it — a foreign package in the interpreter is not this
        # service's supplier. With no closure (no pyproject) fall back to scanning all, so the gate
        # never under-scans in a container.
        if closure and cn not in closure:
            continue
        seen.add(cn)
        verdict, detail = classify_distribution(name, _candidates(dist))
        results.append((name, dist.version, verdict, detail))
    return sorted(results, key=lambda r: r[0].lower())


def main() -> int:
    results = scan()
    bad = [r for r in results if r[2] in (FORBID, UNKNOWN)]
    warns = [r for r in results if r[2] == WARN]

    print(f"License gate — {len(results)} distributions scanned")
    for name, ver, verdict, detail in results:
        if verdict != ALLOW:
            print(f"  [{verdict:7}] {name} {ver} :: {detail}")
    if warns:
        print(f"\n{len(warns)} weak-copyleft (allowed, unmodified use): "
              + ", ".join(r[0] for r in warns))
    if bad:
        print(f"\nFAIL — {len(bad)} disallowed/unknown license(s):")
        for name, ver, verdict, detail in bad:
            print(f"  {verdict}: {name} {ver} :: {detail}")
        return 1
    print("\nPASS — all dependencies are permissive (or safe weak-copyleft).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
