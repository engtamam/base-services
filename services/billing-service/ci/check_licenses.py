#!/usr/bin/env python3
# =============================================================================
# billing-service — License gate (permissive-only, stdlib)
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


# Ordered most-permissive → least. A DISJUNCTION (choose one) takes the most permissive it can;
# a CONJUNCTION (all obligations apply) is bound by its most restrictive term. UNKNOWN sits below
# WARN in a conjunction because an unvetted licence is a reason to stop, but above nothing.
_BY_PERMISSIVENESS = (ALLOW, WARN, UNKNOWN, FORBID)


def _most_permissive(verdicts) -> str:
    s = set(verdicts)
    for v in _BY_PERMISSIVENESS:                 # ALLOW wins, then WARN, then UNKNOWN, then FORBID
        if v in s:
            return v
    return UNKNOWN


def _most_restrictive(verdicts) -> str:
    s = set(verdicts)
    for v in reversed(_BY_PERMISSIVENESS):       # FORBID wins, then UNKNOWN, then WARN, then ALLOW
        if v in s:
            return v
    return UNKNOWN


_OR = re.compile(r"\bOR\b", re.I)
_AND = re.compile(r"\bAND\b", re.I)


def classify_expression(expr: str) -> str:
    """An SPDX License-Expression, where OR / AND are EXPLICIT and remove the ambiguity a bare
    classifier list has. `A OR B` is a choice — take the most permissive operand; `A AND B` binds
    both — take the most restrictive. Parens/WITH are flattened (best-effort): that only widens the
    operand set, and where OR and AND are mixed we fall to the restrictive reading so a forbidden
    term can never be hidden behind an OR."""
    cleaned = expr.replace("(", " ").replace(")", " ")
    has_or, has_and = bool(_OR.search(cleaned)), bool(_AND.search(cleaned))
    if has_or and not has_and:
        return _most_permissive(classify_one(p) for p in _OR.split(cleaned) if p.strip())
    if has_and and not has_or:
        return _most_restrictive(classify_one(p) for p in _AND.split(cleaned) if p.strip())
    if has_or and has_and:                       # mixed: fail safe toward conjunctive
        parts = re.split(r"\bOR\b|\bAND\b", cleaned, flags=re.I)
        return _most_restrictive(classify_one(p) for p in parts if p.strip())
    return classify_one(expr)


def _license_sources(dist: metadata.Distribution) -> tuple[list[str], str, str]:
    """(classifier lines, License-Expression, short License name) — kept SEPARATE because they carry
    different logic: a classifier list is disjunctive, an expression states its own OR/AND."""
    md = dist.metadata
    classifiers = [c for c in md.get_all("Classifier", []) if "License" in c]
    expr = (md.get("License-Expression") or "").strip()
    lic = md.get("License")
    # A short License field is a name; a long one is full text (misleading) — skip long.
    name = lic.strip() if (lic and len(lic) < 60) else ""
    return classifiers, expr, name


def classify_distribution(name: str, candidates: list[str]) -> tuple[str, str]:
    """Classify a DISJUNCTIVE set of licence declarations — PyPI classifier lists are alternatives
    the recipient chooses among (a package offered as `Public Domain` or `BSD` or `GPL` may be taken
    under Public Domain), so the MOST PERMISSIVE verdict is the one it can be used under. Selecting
    the GPL on the user's behalf and then forbidding the package for being GPL is exactly backwards,
    and it fails in the direction that makes people bypass the gate. For an explicit AND, see
    classify_expression — classifier lists have no AND."""
    if name.lower() in OVERRIDES:
        return OVERRIDES[name.lower()], "override"
    detail = "; ".join(candidates)[:80] if candidates else "(no license metadata)"
    return _most_permissive(classify_one(c) for c in candidates), detail


def verdict_for(name: str, dist: metadata.Distribution) -> tuple[str, str]:
    """The verdict for an installed distribution, reading each licence source with its own logic:
    an explicit License-Expression is authoritative (it states OR/AND); otherwise a classifier list
    is disjunctive; otherwise the single License name."""
    classifiers, expr, lic_name = _license_sources(dist)
    parts = classifiers + ([expr] if expr else []) + ([lic_name] if lic_name else [])
    detail = "; ".join(parts)[:80] if parts else "(no license metadata)"
    if name.lower() in OVERRIDES:
        return OVERRIDES[name.lower()], "override"
    if expr:
        return classify_expression(expr), detail
    if classifiers:
        return classify_distribution(name, classifiers)[0], detail
    if lic_name:
        return classify_one(lic_name), detail
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
        verdict, detail = verdict_for(name, dist)
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
