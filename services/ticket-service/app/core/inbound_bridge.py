"""Inbound-channel -> ticket bridge — a GENERIC infrastructure primitive.

A customer message arriving on a channel (WhatsApp today, any channel tomorrow) becomes a ticket: it
opens a new one, or appends to that customer's still-open ticket on that channel. Channel-agnostic by
design — the core takes a NORMALISED InboundMessage and knows nothing about WhatsApp; a source adapter
(app/adapters/inbound_source/*) reads a specific feed and normalises into this shape.

Two invariants that keep it safe to run on a schedule (at-least-once source, retries, re-polls):
  • IDEMPOTENT on the external message id — a re-delivered/re-polled message is ingested once, never
    a duplicate reply. Claimed BEFORE the ticket write and rolled back if that write fails, so a
    duplicate is prevented in the common case and only a hard crash mid-write could drop one.
  • GROUPED per (tenant, channel, sender) via a thread index — one open ticket per customer per
    channel; once it reaches a terminal status the next message opens a fresh ticket.

The sender (a phone number) is never stored raw: TicketService pseudonymises it into raiser_ref, and
the ticket's `ref` records {kind: channel, id: sender} for traceability only.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

_INGESTED = "inbound_ingested"     # (channel:external_id) -> claimed, for idempotency
_THREAD = "inbound_threads"        # (tenant|channel|sender) -> {ticket_id}, the open-thread index


@dataclass
class InboundMessage:
    """A channel message normalised for the bridge. `external_id` is the source's stable id (e.g. a
    WhatsApp wamid) — the idempotency key. `text` is the human body; `subject` is an optional hint."""
    channel: str
    tenant: str
    sender: str
    external_id: str
    text: str = ""
    timestamp: float = 0.0
    subject: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class InboundBridge:
    def __init__(self, *, service, workflow, store, ticket_type: str = "support",
                 subject_template: str = "New {channel} message") -> None:
        self._svc = service                 # TicketService (open/reply/get)
        self._wf = workflow                 # WorkflowRegistry, for the terminal check
        self._store = store                 # DataStore
        self._type = ticket_type
        self._subject_tmpl = subject_template

    @staticmethod
    def _thread_key(tenant: str, channel: str, sender: str) -> str:
        # length-prefixed tenant so one tenant's threads can never collide with another's (see the
        # tenant-scoped-idempotency lesson).
        return f"{len(tenant)}:{tenant}|{channel}|{sender}"

    def _subject(self, msg: InboundMessage) -> str:
        return msg.subject or self._subject_tmpl.format(channel=msg.channel)

    async def _is_terminal(self, ticket) -> bool:
        try:
            return ticket.status in self._wf.require(ticket.type).terminal
        except Exception:  # noqa: BLE001 — an unknown type is not "open"; open a fresh ticket
            return True

    async def _open_ticket(self, tenant: str, ticket_id: str):
        """The customer's ticket if it still accepts messages, else None (missing or terminal)."""
        try:
            t = await self._svc.get(tenant, ticket_id)
        except Exception:  # noqa: BLE001 — TicketNotFound (or gone): treat as no open ticket
            return None
        return None if await self._is_terminal(t) else t

    async def ingest(self, msg: InboundMessage) -> str:
        """-> 'created' | 'appended' | 'skipped'. Skips an unaddressable or already-ingested message."""
        if not (msg.external_id and msg.sender and msg.tenant):
            return "skipped"                        # nothing to key, dedupe, or attribute
        # Tenant-scoped, length-prefixed idempotency key (matches _thread_key): keying on
        # channel+external_id ALONE let one tenant's external_id collide with another's and drop a
        # legitimate message as a duplicate (ticket-r2-06).
        ikey = f"{len(msg.tenant)}:{msg.tenant}|{msg.channel}:{msg.external_id}"
        # Claim ATOMICALLY before the write so a concurrent/re-polled copy cannot double-open. The old
        # get-then-put had a TOCTOU window: two deliveries of the same message could both see "absent"
        # and both open a ticket. cas(expected_version=0) is insert-if-absent — exactly one claimant
        # wins; the loser is a re-delivery and is skipped. Rolled back if the write fails so the
        # message is retried rather than silently lost.
        # `id` == the row key, so the backup can restore it (backup keys each row by row["id"]; a row
        # without one collapses every row under a single None key on restore — the key==id invariant).
        if not await self._store.cas(_INGESTED, ikey, 0, {"id": ikey, "seen": 1, "ts": msg.timestamp}):
            return "skipped"                        # idempotent: seen (or being ingested) already
        try:
            # PSEUDONYMISE the sender before it is persisted anywhere. The raw phone number must never
            # be stored — not in the thread-index key, the ticket ref, or a reply author. The
            # pseudonym is deterministic (salted hash), so the same sender still resolves to the same
            # thread. (raiser is still passed raw to open(), which pseudonymises it into raiser_ref.)
            sref = self._svc.pseudonym(msg.tenant, msg.sender)
            tkey = self._thread_key(msg.tenant, msg.channel, sref)
            idx = await self._store.get(_THREAD, tkey) or {}
            existing = idx.get("ticket_id")
            if not existing:
                # ATOMICALLY reserve the thread slot before opening. The old get-then-put was a TOCTOU:
                # two concurrent messages from the SAME customer both saw an empty index and both opened
                # a duplicate ticket (ticket-r2-04). cas(expected_version=0) is insert-if-absent — the
                # winner opens the ticket and fills in its id; the loser polls for the winner's id.
                if await self._store.cas(_THREAD, tkey, 0, {"id": tkey, "ticket_id": ""}):
                    t = await self._svc.open(
                        tenant=msg.tenant, type=self._type, subject=self._subject(msg),
                        body=msg.text, raiser=msg.sender, ref={"kind": msg.channel, "id": sref})
                    await self._store.put(_THREAD, tkey, {"id": tkey, "ticket_id": t.id})
                    return "created"
                # lost the reservation race — wait for the winner to publish its ticket_id, then append
                for _ in range(100):
                    idx = await self._store.get(_THREAD, tkey) or {}
                    existing = idx.get("ticket_id")
                    if existing:
                        break
                    await asyncio.sleep(0)
            if existing:
                t = await self._open_ticket(msg.tenant, existing)
                if t is not None:
                    await self._svc.reply(tenant=msg.tenant, ticket_id=existing,
                                          author=sref, body=msg.text, by="customer")
                    return "appended"
                # the grouped ticket is terminal/gone → open a fresh one under a new reservation
                t = await self._svc.open(
                    tenant=msg.tenant, type=self._type, subject=self._subject(msg),
                    body=msg.text, raiser=msg.sender, ref={"kind": msg.channel, "id": sref})
                await self._store.put(_THREAD, tkey, {"id": tkey, "ticket_id": t.id})
                return "created"
            # reservation won by nobody we can see (should not happen) → open defensively
            t = await self._svc.open(tenant=msg.tenant, type=self._type, subject=self._subject(msg),
                                     body=msg.text, raiser=msg.sender,
                                     ref={"kind": msg.channel, "id": sref})
            await self._store.put(_THREAD, tkey, {"id": tkey, "ticket_id": t.id})
            return "created"
        except Exception:
            await self._store.delete(_INGESTED, ikey)   # un-claim so a retry re-processes it
            raise

    async def ingest_batch(self, messages: list[InboundMessage]) -> dict[str, int]:
        counts = {"created": 0, "appended": 0, "skipped": 0}
        for m in messages:
            counts[await self.ingest(m)] += 1
        return counts
