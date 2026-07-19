"""Null gate — answers UNKNOWN, never ALLOWED.

This is the default, because a service must boot with zero infrastructure. It is also the single
place in the consent path where the control could be quietly destroyed: a null gate that returned
ALLOWED would mean every service reported "consent checked" while nothing was ever asked. That is
strictly worse than today's state — today nobody asks and everybody knows; then, everybody would
ask and the answer would be a lie.

UNKNOWN and not DENIED, deliberately. DENIED would be equally dishonest in the other direction and
far more annoying: a local run with no compliance-service would refuse every operation, and the
first thing anyone would do is switch the gate off entirely — leaving the production posture
decided by whoever got tired of it. UNKNOWN is the truth (we did not ask anyone), and it hands the
decision to each caller's declared policy, which is where it belongs.

The production boot guard refuses to start on this driver for any service that declares it gates
processing on consent. `null` is for tests and a first local run, never for anywhere real personal
data is handled.
"""
from __future__ import annotations

from app.ports.consent_gate import UNKNOWN, ConsentVerdict


class NullConsentGate:
    driver = "null"

    def __init__(self) -> None:
        self.asked: list[tuple[str, str, str]] = []

    async def check(self, *, tenant: str, subject: str, purpose: str) -> ConsentVerdict:
        # Recorded so a test can prove the gate was REACHED rather than skipped — the difference
        # between "we asked and nothing was configured to answer" and "we never asked at all" is
        # the whole subject of this port.
        self.asked.append((tenant, subject, purpose))
        return ConsentVerdict(
            status=UNKNOWN,
            reason="null consent gate: nothing was asked (by design). Set "
                   "CONSENT_GATE_DRIVER=http and COMPLIANCE_SERVICE_URL to reach compliance-service.",
            purpose=purpose,
        )
