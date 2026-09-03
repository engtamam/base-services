"""LegalGate — does this subject still owe a legal acceptance, checked at mint time.

The split, like CapsResolver:

    compliance owns  "acme published Terms v3, and Sara has not accepted it"  — the legal record.
    identity   owns  "so Sara's token is refused / flagged until she does"    — the enforcement.

Before this port, ToS/Privacy acceptance was a POST-LOGIN UI nag: a shop could register and use the
API with no acceptance recorded, and nothing stopped it. Enforcement has to happen at the MINT — the
one moment a token comes into being — or a route-level check is one new route away from being
bypassed, and refresh would let an unaccepted session live for ever on renewals.

This is why the outstanding check lives here and not in a login handler.
"""
from __future__ import annotations

from typing import Protocol


class LegalGate(Protocol):
    driver: str

    async def outstanding(self, *, subject: str, tenant: str) -> list[dict]:
        """The required legal documents this subject must (re-)accept before proceeding, as the list
        compliance returns (each a dict with at least doc_id / current_version / reason). An empty
        list means "nothing owed". Raises LegalGateUnavailable if compliance cannot be reached — the
        caller decides fail-open vs fail-closed, because an empty list here MUST mean "owes nothing",
        never "I could not ask": reading an outage as "owes nothing" would silently disable the gate.
        """
        ...


class LegalGateUnavailable(Exception):
    """The legal authority (compliance-service) could not be reached.

    Unlike CapsUnavailable this does NOT dictate fail-closed: a legal-consent check that can take
    down ALL logins the instant compliance hiccups is its own outage, and a brief gap is no worse
    than the pre-enforcement world (zero enforcement). The fail mode is therefore an operator knob
    (IDENTITY_LEGAL_GATE_FAIL_MODE): `open` (default, availability-first, logged+audited) or
    `closed` (max legal strength). The one thing neither mode may do is treat unreachable as
    "owes nothing" — that is a silent disable, not a decision.
    """
