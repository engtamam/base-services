"""Erasure — webhook: the person is inside payloads nobody declared a schema for.

This service stores three things that carry other people's data by design:

  * `Event.payload` — the business event we emitted ("user.created" with the user in it).
  * `InboundEvent.payload` — a third party's raw body (Stripe, GitHub), routinely full of PII.
  * `Delivery.response_body` — what the customer's endpoint said back, retained for retry history.

None has a subject column, so erasure means walking the blob (see blob.py). Rows are REDACTED, not
deleted, and the difference matters:

  * The delivery history is how an operator answers "did this webhook ever reach us, and what
    happened?". Deleting the rows would destroy an operational record to remove one field from it.
  * A payload with a key removed can crash a consumer that replays it. Replacing the value with the
    pseudonym keeps the shape intact — an erasure must not become an outage.

What is NOT retained under any basis: nothing here has a legal duty behind it. A webhook payload is
convenience data, so if it holds the person, the person comes out. That is why `retained` is empty
in the ack rather than carrying a basis nobody could defend.

Idempotent: once redacted, the subject no longer matches, so a retry finds nothing and still acks.
"""
from __future__ import annotations

from typing import Any

from app.core.blob import redact
from app.core.pii import subject_ref

_EVENTS = "events"
_INBOUND = "inbound"
_DELIVERIES = "deliveries"
_PAGE = 500


class WebhookErasure:
    service = "webhook-service"
    category = "webhook_payloads"

    def __init__(self, *, store, pii_salt: str, text_keys: frozenset[str] = frozenset(),
                 max_scan: int = 100_000) -> None:
        self._store = store
        self._salt = pii_salt
        self._text_keys = text_keys
        self._max = max_scan

    async def erase(self, tenant: str, subject: str) -> dict[str, Any]:
        ref = subject_ref(self._salt, tenant, subject)
        counts: dict[str, int] = {}
        truncated = False

        for collection, fields, scoped in ((_EVENTS, ("payload",), True),
                                           (_INBOUND, ("payload",), False),
                                           (_DELIVERIES, ("response_body", "error"), True)):
            hits, cut = await self._redact_collection(collection, fields, tenant, subject, ref,
                                                      scoped)
            truncated = truncated or cut
            if hits:
                counts[collection] = hits

        erased = [f"{name}:{n}" for name, n in sorted(counts.items())]
        return {
            "service": self.service,
            "category": self.category,
            "subject_ref": ref,
            "erased": erased,
            "retained": [],          # no legal duty holds a webhook payload
            "already_absent": not erased,
            "truncated": truncated,
        }

    async def _redact_collection(self, collection: str, fields: tuple[str, ...], tenant: str,
                                 subject: str, ref: str, scoped: bool) -> tuple[int, bool]:
        # Inbound events are not tenant-keyed (they arrive before we know whose they are), so that
        # collection is walked unscoped. Everything else is filtered to the tenant — one customer's
        # DSAR must never rewrite another customer's rows.
        filt = {"tenant": tenant} if scoped else {}
        hits = scanned = 0
        cursor = None
        while scanned < self._max:
            rows, cursor = await self._store.find(collection, filt, limit=_PAGE, after=cursor,
                                                  sort=("created", "asc"))
            if not rows:
                break
            for row in rows:
                scanned += 1
                row_hits = 0
                for field in fields:
                    if field not in row:
                        continue
                    # The field NAME is the key for text matching: response_body is a top-level
                    # column, not a nested one, so without this it would never match its own
                    # ERASURE_TEXT_KEYS entry and the person would survive in the response text.
                    new, n = redact(row[field], subject, ref, self._text_keys, _key=field)
                    if n:
                        row[field] = new
                        row_hits += n
                if row_hits:
                    await self._store.put(collection, row["id"], row)
                    hits += row_hits
            if not cursor:
                break
        return hits, scanned >= self._max
