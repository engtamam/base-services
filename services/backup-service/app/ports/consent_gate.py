"""ConsentGate port — ask before processing personal data, and be refused when the answer is no.

compliance-service has served `GET /internal/compliance/consent-check` since Phase 21: default
deny, generation-keyed cache, correct. It had ZERO callers. Not one of the other nineteen services
had a consent knob, a consent adapter, or the string "consent" in any code path — so the platform
shipped a working compliance control that nothing on the platform consulted.

That is the same defect as the empty audit chain, one seam over: the receiving half was built and
the asking half never was. The audit port exists because the chain was empty. This exists because
the answer was never asked for.

WHY A PORT PER SERVICE, NOT A SHARED CLIENT LIBRARY. An adopting team asked us to publish one, and
it is genuinely the thing that would stop everyone hand-writing the same HTTP call. We are not
doing it, because every service here is 100% self-contained by design — a shared client is a shared
dependency, and one upgrade to it becomes twenty coordinated deployments. Instead the port and its
adapters live in the scaffold, so a new service is BORN able to ask, and each carries its own copy
like every other port. The hand-written-call problem is solved; the coupling is not created.

THREE ANSWERS, AND THE THIRD IS THE POINT:

    allowed    consent is on record, unexpired, not withdrawn. Proceed.
    denied     there is no consent, or it was withdrawn or has lapsed. Do NOT proceed.
    unknown    we could not reach compliance-service, or it answered something we cannot parse.

`unknown` is not a third flavour of denied and it is not a quiet allowed. It is the state where the
caller has to make a decision it cannot delegate, and the safe default depends on what the caller
is about to do. Sending a marketing email under `unknown` is a regulatory risk; refusing to serve a
user's own profile under `unknown` is an outage. So the PORT reports honestly and each caller
declares its own posture — see ConsentPolicy below.

WHAT IS NEVER SENT: this asks with the raw subject, because that is the identifier compliance-
service keys consent by. A service that holds only a pseudonym (audit-service keeps subject_ref,
an HMAC with no inverse) CANNOT use this port, and must not fake it by re-deriving the subject —
that would defeat the pseudonymisation. Those services need a different question shape, tracked
separately; a port that quietly accepted either would hide the mismatch rather than resolve it.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

ALLOWED = "allowed"
DENIED = "denied"
UNKNOWN = "unknown"

# What a caller does when the answer is `unknown`. Declared per call site, never globally: the
# right posture is a property of the PROCESSING, not of the deployment.
FAIL_CLOSED = "fail_closed"      # treat unknown as denied — for anything a regulator would ask about
FAIL_OPEN = "fail_open"          # treat unknown as allowed — for processing that is not consent-gated


@dataclass(frozen=True)
class ConsentVerdict:
    """The answer, plus enough context to act on it and to explain it later."""

    status: str                  # ALLOWED | DENIED | UNKNOWN
    reason: str = ""             # granted | no_consent_record | withdrawn | expired | unreachable
    purpose: str = ""
    cached: bool = False

    @property
    def allowed(self) -> bool:
        """Written `== ALLOWED`, never `!= DENIED`.

        A status this port has not seen yet — a value a future compliance-service returns — must
        default to NOT permitting processing. `!= DENIED` would make every unrecognised answer a
        licence to proceed, which is precisely backwards for a control whose whole purpose is to
        stop processing that nobody agreed to.
        """
        return self.status == ALLOWED

    def permits(self, policy: str) -> bool:
        """Should the caller proceed, given its declared posture on `unknown`?"""
        if self.status == ALLOWED:
            return True
        if self.status == UNKNOWN:
            return policy == FAIL_OPEN
        return False


@runtime_checkable
class ConsentGate(Protocol):
    """Ask whether this tenant may process this subject's data for this purpose."""

    driver: str

    async def check(self, *, tenant: str, subject: str, purpose: str) -> ConsentVerdict:
        """Never raises. A gate that threw would turn a compliance question into an outage, and
        the caller would learn nothing about WHY it could not proceed — so an unreachable
        compliance-service is reported as UNKNOWN and the caller's policy decides."""
        ...
