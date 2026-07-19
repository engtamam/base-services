"""Null verifier — verifies nothing, and says ERROR rather than pretending.

Every other `null` adapter on this platform refuses to fake success. This one matters most. A null
sink loses an audit event; a null transport fails to erase. A null verifier that returned VERIFIED
would let ANY caller claim ANY identity — a national id they have never held, a company they do not
own — and everything downstream would believe it. The welcome gift issues, the business account
opens, and the audit log faithfully records that we verified them.

So it returns ERROR: "we could not ask", which is the truth. Not REFUSED, because refusing implies
a provider answered no, and no provider answered at all.

The boot guard refuses this driver in production for any claim type the platform says it verifies.
This exists for tests and for a first local run, never for anywhere a real decision is made.
"""
from __future__ import annotations

from app.ports.verifier import ERROR, Claim, Outcome


class NullVerifier:
    driver = "null"

    def __init__(self) -> None:
        self.claims: list[Claim] = []

    async def verify(self, claim: Claim) -> Outcome:
        # Recorded so a test can prove the verifier was REACHED rather than skipped — the
        # difference between "we asked and got nothing" and "nobody ever asked".
        self.claims.append(claim)
        return Outcome(
            status=ERROR,
            provider=self.driver,
            detail=("null verifier: nothing was checked and nothing is known (by design). "
                    "Configure a real provider for this claim type."),
        )
