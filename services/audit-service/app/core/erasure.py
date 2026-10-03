"""Erasure — audit-service erases NOTHING, and that is the correct answer.

Every other service, asked to erase a subject, deletes something. Audit deletes nothing, and this
module exists to say so out loud, with a legal basis attached, rather than to stay silent.

Why nothing:

  * The chain is the product. Every event carries `prev_hash`; redact one field of one event and
    `verify()` fails from that seq onward, forever. An audit log you can quietly edit to make a
    person disappear is an audit log that proves nothing about anyone.

  * There is nothing personal to remove. Events carry a `subject_ref` pseudonym, never a name, an
    email, or a raw IP. Once identity-service deletes the linkage, every `sub_…` in this chain is
    an orphan: it still proves that *someone* did a thing, in order, untampered — it just cannot
    be resolved to a human. Which is exactly what both laws want.

So the ack this service returns is: erased nothing, RETAINED everything, basis `security_audit`,
and — the part that matters — the chain still verifies. "We kept it" is an answer with a reason
attached, never a silence.

The one thing that would break this design is an event that smuggled real PII into `metadata`.
PiiPolicy (app/core/pii_policy.py) refuses that at WRITE time, because by the time it is in the chain
it is permanent.
"""
from __future__ import annotations

from typing import Any

RETENTION_BASIS = "security_audit"


class AuditErasure:
    """The erasure ack for a store that is, by design, unerasable."""

    service = "audit-service"
    category = "audit_log"

    def __init__(self, *, audit) -> None:
        self._audit = audit

    async def erase(self, tenant: str, subject_ref_value: str,
                    partition: str | None = None) -> dict[str, Any]:
        """Erase nothing; retain everything; PROVE the chain still verifies.

        The proof is the point. A service that says "I retained it for security" and cannot show
        the record is still intact has given an excuse, not an ack. In AUDIT_PARTITION=tenant mode
        the subject's events live in the TENANT's partition, so we must verify THAT chain — checking
        the hardcoded 'global' partition (often empty in tenant mode) proved an unrelated chain.
        """
        part = partition if partition is not None else self._audit._partition(tenant)
        report = await self._audit.verify(part)
        intact = bool(report.get("ok"))

        return {
            "service": self.service,
            "subject_ref": subject_ref_value,
            "erased": [],
            "retained": [
                {
                    "what": "audit_events",
                    "basis": RETENTION_BASIS,
                    "why": ("append-only tamper-evident chain; the events carry a pseudonym, "
                            "not an identity, and are unlinkable once identity erases the mapping"),
                }
            ],
            "chain_verifies": intact,
            "already_absent": False,
        }


# NOTE (audit-009): assert_no_pii() was removed — a dead, never-called enforcement path with a stale
# hardcoded field list. PiiPolicy (app/core/pii_policy.py), applied at the record boundary and driven by
# AUDIT_PII_FIELDS, is the single live enforcement of "no raw identity enters the chain".
