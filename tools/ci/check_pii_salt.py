#!/usr/bin/env python3
"""safety:pii-salt — a service that pseudonymises must refuse to start without the salt.

Every service writes `subject_ref` = HMAC(PII_SALT, tenant|subject): the pseudonym that lets an
immutable audit row, ledger entry or invoice outlive the person named in it. `core/pii.py` refuses
an empty salt rather than emitting a digest anyone could reverse by hashing every email address.

That refusal is right and arrives late. It fires at CALL time, so a service with no salt starts
cleanly, passes every health check, serves traffic for weeks, and then 500s the first time a real
person touches a path that pseudonymises — a DSAR, or the first audited action of the day.

Two services out of twenty-one guarded against that. The other nineteen declared PII_SALT and
never checked it, and nothing anywhere noticed: each service was internally consistent, the setting
existed, the code that used it was correct. An adopter found it by reading files one at a time.

So the rule is mechanical now. If a service reads PII_SALT, it must:

    1. define assert_pii_config_ok() in app/config.py, which raises when the salt is empty in
       production, and
    2. CALL it from app/main.py — because a guard nobody calls is the same defect wearing the
       costume of a fix, and this repository has found that shape often enough to stop trusting
       that writing one implies running it.

WHAT THIS GATE CANNOT CHECK: that every service was given the SAME salt. Two different non-empty
salts fold one person to two pseudonyms and every service still boots, still works, and silently
loses the linkage that erasure and legal holds depend on. That is a deployment invariant, not a
source-code one — PII_SALT is frozen in config/product.identifiers.lock.json for that reason.
"""
from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
SERVICES = ROOT / "services"

GUARD = "assert_pii_config_ok"


def _strip_docstring(body: str) -> str:
    """The method body with its leading docstring removed, so prose cannot satisfy a code check."""
    m = re.search(r'^\s*(?:"""|\'\'\')', body[body.index("\n") + 1:], re.M)
    if not m:
        return body
    rest = body[body.index("\n") + 1:]
    quote = rest[m.start():m.end()].strip()
    close = rest.find(quote, m.end())
    return rest[close + 3:] if close != -1 else rest


def main() -> int:
    problems: list[str] = []
    checked = 0

    for cfg in sorted(SERVICES.glob("*/app/config.py")):
        service = cfg.parts[-3]
        text = cfg.read_text(encoding="utf-8")
        if "PII_SALT" not in text:
            continue                      # does not pseudonymise; nothing to guard
        checked += 1

        if f"def {GUARD}" not in text:
            problems.append(
                f"{service}: reads PII_SALT but defines no {GUARD}() — it would start without a "
                f"salt and 500 on the first pseudonymised write")
            continue

        # The guard must actually REFUSE, not merely exist. A body that logs a warning and
        # continues is the failure this gate was written to stop, one indirection later.
        #
        # THE DOCSTRING IS STRIPPED FIRST, and that is not tidiness. The first version of this
        # gate searched the whole method for "raise" — and the guard's own docstring contains the
        # word "raises", explaining the very failure it prevents. So a guard mutated to log a
        # warning instead of raising still passed, because its prose said the right thing while
        # its code did not. Caught by mutation-testing this file; a gate is only worth what it
        # rejects.
        body = text[text.index(f"def {GUARD}"):]
        body = body[:body.index("\n    def ", 1)] if "\n    def " in body[1:] else body
        body = _strip_docstring(body)
        if "raise" not in body or "PII_SALT" not in body:
            problems.append(
                f"{service}: {GUARD}() does not raise on an empty PII_SALT — a guard that warns "
                f"and continues is not a guard")
            continue
        if "is_production" not in body:
            problems.append(
                f"{service}: {GUARD}() does not scope its refusal to production — developers "
                f"would be unable to run the service without setting a production secret")
            continue

        main_py = cfg.parent / "main.py"
        if not main_py.exists():
            problems.append(f"{service}: no app/main.py, so {GUARD}() can never be called")
            continue
        called = re.search(rf"\.{GUARD}\s*\(", main_py.read_text(encoding="utf-8"))
        if not called:
            problems.append(
                f"{service}: defines {GUARD}() and never calls it from main.py — built, correct, "
                f"and inert")

    if problems:
        print("      a service can start without the salt its pseudonyms are built from:")
        for p in problems:
            print(f"        {p}")
        print(f"      Fix: define {GUARD}() in app/config.py and call it in app/main.py "
              f"beside assert_audit_config_ok().")
        return 1

    print(f"      {checked} services refuse to start in production without PII_SALT")
    return 0


if __name__ == "__main__":
    sys.exit(main())
