"""Erasure — connector: the dead-letter queue is the biggest pile of other people's PII we hold.

`SyncRecord.record` is a whole CRM/ERP row — a Salesforce contact, a HubSpot lead — parked because
its sync failed. The mapping layer is explicitly built to move fields like `properties.email`, so
these rows are contact data by definition, and they persist indefinitely after a failed sync.

Here the DLQ row is DELETED rather than redacted, which is the opposite of the webhook decision and
for a concrete reason: a DLQ entry exists to be RETRIED. A redacted record would be replayed into
the customer's CRM with a pseudonym where the email used to be — writing corrupted data into a
downstream system we do not own, in the name of privacy. There is nothing to preserve in the row
once the person is removed from it, because the row IS the person's record.

`sync_seen` keys are left alone deliberately. They are idempotency markers — a key and nothing
else, carrying no personal data — and deleting them would make a later replay re-import the very
records we just erased. Removing a safeguard is not erasure.

Idempotent: the second pass finds nothing and still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.blob import redact
from app.core.pii import subject_ref

_DLQ = "sync_dlq"
_PAGE = 500


class ConnectorErasure:
    service = "connector-service"
    category = "synced_records"

    def __init__(self, *, store, pii_salt: str, text_keys: frozenset[str] = frozenset(),
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._text_keys = text_keys
        self._max = max_scan

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        deleted = scanned = 0
        cursor = None

        while scanned < self._max:
            rows, cursor = await self._store.find(_DLQ, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("created", "asc"))
            if not rows:
                break
            for row in rows:
                scanned += 1
                # `record` holds the person; `error` may quote them back in a message.
                _, hits = redact(row.get("record"), subject, ref, self._text_keys, _key="record")
                if not hits:
                    _, hits = redact(row.get("error", ""), subject, ref, self._text_keys,
                                     _key="error")
                if hits:
                    await self._store.delete(_DLQ, row["id"])
                    deleted += 1
            if not cursor:
                break

        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": [f"dead_lettered_records:{deleted}"] if deleted else [],
            "retained": [],          # a failed sync payload carries no legal duty
            "already_absent": deleted == 0,
            "truncated": scanned >= self._max,
        }
