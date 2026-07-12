#!/usr/bin/env python
"""Gate: a service that holds a data subject MUST be able to erase them.

THE FOOTGUN THIS EXISTS FOR.

compliance-service derives a DSAR's completion from the acks of the targets it KNOWS about. A
service that holds subject data but was never registered is never asked, never acks, and is
therefore never MISSED: the derivation sees "every target acked", the DSAR closes green, and the
person is told they were erased. They were not.

It is the same shape as every other defect in this repo — the check passes because it was looking
at an incomplete list. The list cannot be trusted to whoever remembered to update it. It has to be
checked against the CODE.

So, for every service:

  1. If its code stores a data subject (a `subject`/`user`/`owner`/`actor` field, or a users
     collection), it MUST ship `app/core/erasure.py`. No handler = the erasure silently misses it.

  2. Anything it RETAINS must name a legal basis from a closed list. "We kept it" is an answer with
     a reason attached, never a silence — and an unknown basis is someone inventing one.

  3. A service that writes into an IMMUTABLE record (the audit chain) must not write raw PII into
     it. There is no later fix: once hashed, it is permanent.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

# A service whose code contains one of these is holding a data subject.
_SUBJECT_MARKERS = (
    re.compile(r'^\s*subject\s*:', re.MULTILINE),          # a dataclass field
    re.compile(r'["\']subject["\']\s*:'),                   # a dict key
    re.compile(r'\bget_by_username\b'),
    re.compile(r'^\s*owner\s*:', re.MULTILINE),
)

# The only bases anyone may retain data under. Inventing a new one is how "we kept it" becomes
# unfalsifiable.
_ALLOWED_BASES = {"tax_retention", "legal_hold", "security_audit", "consent_proof",
                  "delivery_proof", "fraud_prevention"}

# Services that legitimately hold NO data subject (infra / routing only).
_NO_SUBJECT = {"api-gateway"}


def holds_subject(svc: pathlib.Path) -> str | None:
    core = svc / "app" / "core"
    if not core.is_dir():
        return None
    for path in sorted(core.rglob("*.py")):
        if path.name in ("pii.py", "erasure.py"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for marker in _SUBJECT_MARKERS:
            if marker.search(text):
                return f"{path.relative_to(svc).as_posix()}"
    return None


def declared_bases(svc: pathlib.Path) -> set[str]:
    erasure = svc / "app" / "core" / "erasure.py"
    if not erasure.is_file():
        return set()
    text = erasure.read_text(encoding="utf-8")
    return set(re.findall(r'"basis"\s*:\s*([A-Z_]+|"[a-z_]+")', text)) | \
        set(re.findall(r'RETENTION_BASIS\s*=\s*"([a-z_]+)"', text))


def main() -> int:
    problems: list[str] = []
    services = sorted(p for p in SERVICES.iterdir()
                      if p.is_dir() and p.name != "_template" and (p / "app").is_dir())

    holders = 0
    for svc in services:
        where = holds_subject(svc)
        if where is None or svc.name in _NO_SUBJECT:
            continue
        holders += 1

        erasure = svc / "app" / "core" / "erasure.py"
        if not erasure.is_file():
            problems.append(
                f"{svc.name}: holds a data subject ({where}) but has no app/core/erasure.py — "
                f"compliance would never ask it, it would never ack, and the DSAR would close "
                f"GREEN with the person's data still here.")
            continue

        for basis in declared_bases(svc):
            clean = basis.strip('"')
            if clean.isupper():          # a constant name, resolved above; skip
                continue
            if clean not in _ALLOWED_BASES:
                problems.append(
                    f"{svc.name}: retains data under an unknown basis {clean!r} — "
                    f"allowed: {', '.join(sorted(_ALLOWED_BASES))}")

    for p in problems:
        print(f"  {p}")
    if problems:
        print(f"\npii: FAIL — {len(problems)} service(s) could silently miss an erasure")
        return 1
    print(f"pii: PASS — {holders} service(s) hold a data subject; every one can erase them, "
          f"and every retention names a legal basis")
    return 0


if __name__ == "__main__":
    sys.exit(main())
