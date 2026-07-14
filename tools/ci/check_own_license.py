#!/usr/bin/env python
"""Gate: the SOFTWARE is proprietary. Its DEPENDENCIES are permissive. Two rules, opposite ways.

These two are constantly confused, and confusing them is how a company gives its product away.

  * A DEPENDENCY must be permissive (MIT/BSD/Apache-2.0/ISC/PSF). A copyleft dependency can
    force us to publish our source. That rule is enforced by services/*/ci/check_licenses.py.

  * The SERVICE ITSELF must be proprietary. /LICENSE says it plainly: no licence is granted, and
    use requires a signed agreement. The business model is a per-company licence to one named
    project — no resale, no reuse elsewhere.

Every service's pyproject declared `license = "Apache-2.0"`.

That is not a typo, it is a GRANT — the most permissive one there is, allowing anyone holding the
artifact to use, modify, redistribute and SELL it. And it does not stay in the repo: the string is
baked into the wheel's METADATA and into the image layers. A licensee handed that image, whose
lawyer reads `License: Apache-2.0` in the metadata, has a real argument that they were granted
Apache-2.0 rights — while /LICENSE, sitting in a repo they may never have seen, says the opposite.

A contract that contradicts itself is argued in the direction of whoever holds the copy. So the
declared licence of our own packages is checked, in CI, against the licence we actually mean.
"""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# The licence our own packages must declare. Config, because the day this repo is sold, dual
# licensed, or relicensed, the answer changes — and it must change in ONE place, not eighteen.
EXPECTED = os.environ.get("OWN_LICENSE", "LicenseRef-Proprietary")

# Declaring any of these on OUR OWN package is a grant to the world. They are correct for a
# dependency and catastrophic for the product.
GRANTS = {"apache-2.0", "mit", "bsd-3-clause", "bsd-2-clause", "isc", "mpl-2.0",
          "gpl-3.0", "agpl-3.0", "lgpl-3.0", "unlicense", "cc0-1.0"}


def main() -> int:
    if not SERVICES.is_dir():
        print(f"FATAL: no services/ under {ROOT} — the gate found nothing to check")
        return 1

    problems: list[str] = []
    checked = 0
    for pp in sorted(SERVICES.glob("*/pyproject.toml")):
        svc = pp.parent.name
        m = re.search(r'^\s*license\s*=\s*["\']([^"\']+)["\']', pp.read_text(encoding="utf-8"),
                      re.MULTILINE)
        if not m:
            problems.append(f"{svc}: pyproject declares no `license` — say what it is, explicitly")
            continue
        checked += 1
        declared = m.group(1).strip()
        if declared.lower() in GRANTS:
            problems.append(
                f"{svc}: pyproject declares license = {declared!r} — that is a GRANT. It ships in "
                f"the wheel METADATA and the image layers, so anyone holding the artifact can "
                f"argue they may use, modify, resell and redistribute it. /LICENSE says no licence "
                f"is granted. Expected {EXPECTED!r}.")
        elif declared != EXPECTED:
            problems.append(
                f"{svc}: pyproject declares license = {declared!r}, expected {EXPECTED!r} "
                f"(set OWN_LICENSE if the product's licence really did change)")

    if not checked:
        # An empty set passing green is the recurring defect of this repository.
        print("FATAL: no service pyproject files found — a licence gate with no subject cannot pass")
        return 1

    if (ROOT / "LICENSE").is_file():
        body = (ROOT / "LICENSE").read_text(encoding="utf-8").lower()
        if "not open source" not in body and "no license is granted" not in body:
            problems.append(
                "LICENSE no longer says the software is proprietary — if that is deliberate, "
                "change OWN_LICENSE too, so the two cannot disagree")
    else:
        problems.append("there is no LICENSE file at the repo root — silence is not a reservation")

    if problems:
        print(f"own-license — {len(problems)} problem(s):\n")
        for p in problems:
            print(f"  {p}")
        print("\nThe dependencies must be permissive. The product must not be. They are not the "
              "same rule, and mixing them up gives the product away.")
        return 1

    print(f"own-license: PASS — {checked} services declare {EXPECTED!r}; "
          f"LICENSE grants nothing without a signed agreement")
    return 0


if __name__ == "__main__":
    sys.exit(main())
