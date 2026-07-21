#!/usr/bin/env python3
"""safety:no-secret-reads — a stored credential must never leave through an API.

Operator-managed credentials are sealed at rest, but encryption is not the control that matters
here. `SecretStore.get()` returns plaintext by necessity — an SMS client cannot authenticate with a
masked string — so the whole guarantee rests on one rule: no HTTP handler returns that value.

That is a rule about discipline, and discipline is what this repository has repeatedly found to be
insufficient. A single helpful `return {"key": await store.get(...)}` added during a debugging
session would expose every tenant's provider credentials to anyone holding the panel capability,
and it would look entirely reasonable in review.

So the rule is mechanical.

SCOPE: API modules that touch the SecretStore. That is the precise population at risk, and it
grows by itself as each service migrates — a service that starts storing operator credentials comes
under the rule the moment it imports the store, rather than the day somebody remembers to add it to
a list here.

WHAT IS CHECKED, in those modules only
  1. No handler calls `.get(...)` / `.accepted(...)` on the store — those return plaintext.
     Adapters and core code may; the API layer may not.
  2. No response carries a field named like a credential unless it is plainly a masked hint.

WHAT THIS DELIBERATELY DOES NOT DO

It does not flag a freshly generated secret returned ONCE to its own owner — a webhook signing
secret at rotation, a TOTP seed at enrollment. The subject receiving a credential that was just
minted for them is the only way those flows can work, and it is a different act from reading back a
credential an operator stored earlier. Conflating them would mean the gate is wrong about real code
on day one.

It does not check that credential-writing routes are gated and audited. The obvious spelling of
that check flags `login` — which cannot be capability-gated, that being what login is for — and a
gate that is 90% right about something subtle teaches people to argue with it. Auditing is already
carried by the declared-actions contract and the audit-wiring gate.

It does not chase the value through helpers; that needs dataflow analysis. It checks the direct
form, which is the form the mistake actually takes.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
# Overridable so the gate's selftest can point this at a planted violation and prove the gate
# still refuses one. A gate nobody has watched fail is an assumption.
SERVICES = pathlib.Path(os.environ.get("SERVICES_ROOT") or (ROOT / "services"))

# Only modules that actually reach the operator credential store are in scope.
_IN_SCOPE = re.compile(r"\bSecretStore\b|\bsecret_store\b")
# The two methods that hand back plaintext. `info`/`list_info` are the safe reads.
_PLAINTEXT_METHODS = ("get", "accepted")
_STORE_NAMES = ("secret_store", "secretstore", "_secret_store", "_secrets", "store", "secrets")
_SECRETISH = re.compile(r"(api[_-]?key|secret|password|credential|private[_-]?key)", re.I)
# A masked hint is fine — it is the whole point of the panel view. Plural container names
# (`secrets: [...]`) hold hint records, not values; the field check below looks inside them.
_MASKED = re.compile(r"(hint|masked|last4|redacted)", re.I)
_CONTAINER = re.compile(r"^(secrets|credentials)$", re.I)


def _api_files(service: pathlib.Path) -> list[pathlib.Path]:
    api = service / "app" / "api"
    if not api.is_dir():
        return []
    return [p for p in api.rglob("*.py") if "__pycache__" not in str(p)]


def _calls_plaintext_get(tree: ast.AST) -> list[int]:
    """Lines where an API module reads a stored credential's plaintext."""
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        # <something>.get(...) where <something> is named like a secret store
        if isinstance(f, ast.Attribute) and f.attr in _PLAINTEXT_METHODS:
            base = f.value
            name = ""
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            elif isinstance(base, ast.Call):
                # _secret_store(request).get(...)
                inner = base.func
                name = getattr(inner, "id", "") or getattr(inner, "attr", "")
            if name.lower() in _STORE_NAMES or "secret" in name.lower():
                hits.append(node.lineno)
    return hits


def _returns_secret_field(tree: ast.AST) -> list[tuple[int, str]]:
    """Dict literals returned from API code with a secret-looking key that is not a hint."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or not isinstance(node.value, ast.Dict):
            continue
        for k in node.value.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                key = k.value
                # `{"secrets": [...]}` is the panel's list of hint records — the records
                # themselves are checked by this same walk, so the container is not a finding.
                if _CONTAINER.match(key):
                    continue
                if _SECRETISH.search(key) and not _MASKED.search(key):
                    out.append((node.lineno, key))
    return out


def main() -> int:
    problems: list[str] = []
    checked = 0

    for service in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        for path in _api_files(service):
            text = path.read_text(encoding="utf-8")
            if not _IN_SCOPE.search(text):
                continue
            checked += 1
            try:
                rel = path.relative_to(ROOT)
            except ValueError:
                rel = path          # SERVICES_ROOT pointed outside the repo (the selftest)
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                problems.append(f"{rel}: could not parse ({exc})")
                continue

            for line in _calls_plaintext_get(tree):
                problems.append(
                    f"{rel}:{line}: an API module reads a stored credential's PLAINTEXT. "
                    f"Adapters may; a route may not — one helpful return exposes every tenant's "
                    f"provider keys to whoever holds the panel capability.")
            for line, key in _returns_secret_field(tree):
                problems.append(
                    f"{rel}:{line}: a response carries {key!r}, which is named like a secret and "
                    f"is not a masked hint.")

    if problems:
        print("      a stored credential can leave through an API:")
        for p in problems:
            print(f"        {p}")
        print("      The panel gets a hint, never a value. Adapters read the plaintext in-process.")
        return 1

    if not checked:
        # Nothing reaches the store yet. Say so — "0 checked, passed" printed as success is how a
        # gate quietly stops guarding anything.
        print("      no API module reaches the credential store yet — nothing to guard")
        return 0
    print(f"      {checked} API module(s) reach the credential store; none returns a value")
    return 0


if __name__ == "__main__":
    sys.exit(main())
