#!/usr/bin/env python3
# =============================================================================
# api-gateway — License gate (permissive-only, stdlib)
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


def scan() -> list[tuple[str, str, str, str]]:
    results: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
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
