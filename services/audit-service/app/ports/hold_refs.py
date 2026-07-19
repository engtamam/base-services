"""HoldRefs port — which subjects must NOT be purged, expressed as pseudonyms.

This service cannot use compliance-service's per-subject hold-check, and the reason is structural
rather than an oversight. Its rows carry `actor` as a subject_ref — an HMAC with no inverse — so
there is no raw subject to ask about, and taking the salt to re-derive one would defeat the
pseudonymisation the whole design rests on. Its purge also walks partitions by TIMESTAMP, so a
per-row question would mean one HTTP call per event.

So the question is inverted: fetch every held ref for a partition ONCE, and intersect locally.
Both sides derive subject_ref from the same frozen product PII_SALT, so the sets are comparable
without either side ever seeing the other's identifiers.

`complete` is the safety property, not a detail. A partial list means we cannot tell which events
are protected, and a purge on a partial answer destroys evidence somebody is legally obliged to
keep. The contract is that an incomplete answer ABORTS the purge for that partition.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class HoldRefs(Protocol):
    driver: str

    async def for_tenant(self, tenant: str) -> dict[str, Any] | None:
        """`{"refs": [...], "complete": bool}`, or None if it could not be obtained.

        Never raises. None and `complete: false` mean the same thing to the caller — do not purge
        this partition — and both are reported rather than swallowed.
        """
        ...
