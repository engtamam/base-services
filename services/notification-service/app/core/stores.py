"""Repositories over the generic DataStore port (memory today, postgres by config)."""
from __future__ import annotations

import time

from app.core.models import Delivery, InAppItem, Preference, Template
from app.ports.datastore import DataStore

_TEMPLATES = "templates"
_PREFS = "preferences"
_DELIVERIES = "deliveries"
_INAPP = "inapp"
_IDEMPO = "notify_idempo"    # idempotency_key -> {result, ts}
_WA_NUMBER_INDEX = "wa_number_index"   # phone_number_id -> {tenant} (routing map, not a secret)
_RECEIPTS = "notify_receipts"  # append-only receipt-event log, ordered by receipt_ts (catch-up read)
_RECEIPT_KIND = "receipt"      # constant partition key so the ordered read is a filtered query, not a scan
_INBOUND = "wa_inbound"        # append-only inbound-message log, ordered by timestamp (catch-up read)
_INBOUND_KIND = "inbound"      # constant partition key — same reason as _RECEIPT_KIND
# WhatsApp Coexistence catch-up logs (owner's own-app activity on the same number). One collection +
# constant kind each, exactly like _INBOUND, so every read is an indexed ordered query, never a scan.
_ECHOES = "wa_msg_echoes"
_ECHO_KIND = "echo"
_HISTORY = "wa_history"
_HISTORY_KIND = "history"
_CONTACTS = "wa_contacts"
_CONTACT_KIND = "contact"


class TemplateStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    @staticmethod
    def _key(key: str, locale: str) -> str:
        return f"{key}::{locale}"

    async def upsert(self, tmpl: Template) -> Template:
        await self._store.put(_TEMPLATES, self._key(tmpl.key, tmpl.locale), tmpl.to_dict())
        return tmpl

    async def get(self, key: str, locale: str, fallback: str = "en") -> Template | None:
        row = await self._store.get(_TEMPLATES, self._key(key, locale))
        if row is None and locale != fallback:
            row = await self._store.get(_TEMPLATES, self._key(key, fallback))
        return Template.from_dict(row) if row else None

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: the template catalogue is global - one row per template, not per tenant.
        return await self._store.scan_all(_TEMPLATES, limit)


class PreferenceStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def get(self, user: str) -> Preference:
        row = await self._store.get(_PREFS, user)
        return Preference.from_dict(row) if row else Preference(user=user)

    async def set(self, pref: Preference) -> Preference:
        await self._store.put(_PREFS, pref.user, pref.to_dict())
        return pref


class DeliveryStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def record(self, d: Delivery) -> Delivery:
        await self._store.put(_DELIVERIES, d.id, d.to_dict())
        return d

    async def list(self, limit: int = 100) -> list[dict]:
        # sweeper: an operator's view of recent delivery attempts across every tenant. Bounded
        # by the caller's limit, and not reachable from any tenant-facing route.
        return await self._store.scan_all(_DELIVERIES, limit)

    async def by_tenant(self, tenant: str, limit: int = 100, *, channel: str = "") -> list[dict]:
        """Recent deliveries for ONE tenant, newest first, optionally one channel. Both filters are
        pushed into the datastore, so this costs one tenant's rows — never a scan of every tenant's —
        and backs the operator diagnostic ("why is this shop's WhatsApp broken"). An empty tenant
        returns nothing rather than a cross-tenant scan: an unfiltered dump is not what this answers."""
        if not tenant:
            return []
        where = {"tenant": tenant}
        if channel:
            where["channel"] = channel
        rows, _ = await self._store.find(_DELIVERIES, where, limit=limit, sort=("ts", "desc"))
        return rows

    async def due(self, status: str, limit: int) -> list[Delivery]:
        """Deliveries waiting for another attempt. Pushed down by status — never a scan.

        The caller filters on `next_attempt_ts` in Python: the DataStore `where` is equality-only
        by design (no operators means nothing to inject), so "is it time yet" cannot be pushed
        down. That is fine — the set is already narrowed to `queued`, which is a small, bounded
        set even during an outage, and it shrinks as the queue drains.
        """
        rows, _ = await self._store.find(_DELIVERIES, {"status": status}, limit=limit,
                                         sort=("ts", "asc"))
        return [Delivery.from_dict(r) for r in rows]

    async def save(self, d: Delivery) -> Delivery:
        await self._store.put(_DELIVERIES, d.id, d.to_dict())
        return d

    # ── receipts: match a webhook status back to the send it belongs to ────────
    async def by_provider_message_id(self, wamid: str) -> Delivery | None:
        """The delivery a receipt belongs to, found by the wamid both sides carry. Filtered read,
        never a scan — the id is the join key persisted at send time."""
        if not wamid:
            return None
        rows, _ = await self._store.find(_DELIVERIES, {"provider_message_id": wamid}, limit=1)
        return Delivery.from_dict(rows[0]) if rows else None

    async def apply_receipt(self, wamid: str, receipt_status: str, ts: float,
                            seq: str = "") -> str | None:
        """Record what Meta says HAPPENED to a message: update its delivery AND append a receipt EVENT
        to the ordered catch-up log. Returns the delivery id, or None if the wamid matches nothing here
        (a receipt for a send we never made — never invented).

        Monotonic on the delivery: a later `read` is not overwritten by an out-of-order `delivered`,
        and the receipt axis stays separate from the send-state machine, so recording a receipt never
        disturbs `sent`/`queued`/`failed`. The event log is APPEND-ONLY (every state is its own row),
        so a consumer reading the log forward sees `delivered` then `read`, each exactly once."""
        d = await self.by_provider_message_id(wamid)
        if d is None:
            return None
        if ts >= d.receipt_ts:
            d.receipt_status, d.receipt_ts = receipt_status, ts
            await self._store.put(_DELIVERIES, d.id, d.to_dict())
        # Append-only event, keyed by a monotonic seq so the ordered forward read is stable and every
        # state change is a distinct row (id would collapse delivered+read into one).
        eid = seq or f"{d.id}:{receipt_status}:{ts}"
        await self._store.put(_RECEIPTS, eid, {
            "id": eid, "kind": _RECEIPT_KIND, "delivery_id": d.id, "provider_message_id": wamid,
            "receipt_status": receipt_status, "receipt_ts": ts, "recipient": d.recipient,
            "template_key": d.template_key})
        return d.id

    async def changed_since(self, *, after: str | None, limit: int) -> tuple[list[dict], str | None]:
        """The ordered catch-up read: receipt events forward from a cursor, oldest first, bounded.

        The bus is at-most-once — a pod that restarts through a receipt hears it never — so a consumer
        reconciles by reading this log forward and persisting the returned cursor as its watermark. The
        log is append-only and ordered by receipt_ts, so cursor pagination reads every receipt exactly
        once and never misses one behind the watermark. Filtered on a constant kind (not an empty
        `where`), so it stays an indexed, ordered read and never a scan (safety:scans)."""
        return await self._store.find(_RECEIPTS, {"kind": _RECEIPT_KIND}, limit=limit, after=after,
                                      sort=("receipt_ts", "asc"))

    # ── idempotency (at-least-once callers must not double-send) ───────────────
    @staticmethod
    def _idempo_key(tenant: str, key: str) -> str:
        """Scope the dedupe record to the TENANT. The idempotency_key is CALLER-SUPPLIED, so two
        tenants presenting the same key (an order number, an invoice id) would otherwise collide on
        one global slot: the second caller is told `duplicate` and handed the FIRST tenant's result
        while its own send never goes out (a dropped OTP), or worse reads another tenant's payload.
        The tenant is length-prefixed so a key containing the '|' delimiter cannot be crafted to
        land in another tenant's slot (len('a')+'a|b|c' ≠ len('a|b')+'a|b|c')."""
        return f"{len(tenant)}:{tenant}|{key}"

    async def idempotent_get(self, key: str, *, tenant: str, now: float, ttl: int) -> dict | None:
        row = await self._store.get(_IDEMPO, self._idempo_key(tenant, key))
        if not row:
            return None
        if ttl and now and (now - float(row.get("ts", 0) or 0)) > ttl:
            return None          # window elapsed → treat as a fresh send (bounded memory)
        return row.get("result")

    async def remember_idempotent(self, key: str, result: dict, now: float, *, tenant: str) -> None:
        await self._store.put(_IDEMPO, self._idempo_key(tenant, key), {"result": result, "ts": now})


class InboundStore:
    """A durable, replayable log of INBOUND WhatsApp messages — the missed-delivery counterpart to
    the receipt catch-up log.

    Inbound `whatsapp.message` events are published to the bus, but the bus is at-most-once: a pod
    that restarts through one hears it never, and a missed inbound message is a customer whose reply
    is silently never answered, with no symptom anywhere. So every inbound message is ALSO appended
    here and read forward from a watermark, exactly like receipts — reconciliation, not a second
    delivery path.

    Keyed by the wamid (Meta's message id), so it is idempotent: Meta re-delivers a webhook on any
    non-2xx, and a re-delivery overwrites the same row rather than appending a duplicate — closing the
    duplicate-delivery half of the risk here too. Ordered by the message timestamp (stored numeric so
    the forward read orders correctly), with the wamid as the keyset tiebreaker for same-second
    messages, so the catch-up read returns every message exactly once behind the cursor.
    """

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def record(self, *, wamid: str, sender: str, timestamp: float, msg_type: str,
                     tenant: str, phone_number_id: str, waba_id: str, message: dict) -> bool:
        """Append (idempotently, by wamid) one inbound message. Returns False for a message with no
        wamid — nothing to key or dedupe on — rather than storing an unaddressable row."""
        if not wamid:
            return False
        await self._store.put(_INBOUND, wamid, {
            "id": wamid, "kind": _INBOUND_KIND, "wamid": wamid, "from": sender,
            "timestamp": timestamp, "type": msg_type, "tenant": tenant,
            "phone_number_id": phone_number_id, "waba_id": waba_id, "message": message})
        return True

    async def changed_since(self, *, after: str | None, limit: int) -> tuple[list[dict], str | None]:
        """Inbound messages forward from a cursor, oldest first, bounded — the catch-up read. Filtered
        on a constant kind (never an empty where), so it stays an indexed, ordered read, not a scan."""
        return await self._store.find(_INBOUND, {"kind": _INBOUND_KIND}, limit=limit, after=after,
                                      sort=("timestamp", "asc"))


class CoexistenceStore:
    """Durable, replayable logs for WhatsApp COEXISTENCE — a shop running its own WhatsApp Business app
    on the SAME number it connected to us. Three webhook fields carry what the owner's phone does
    outside us, and they are exactly what an AI/reminders consumer reading "the conversation" must not
    miss:
      • message_echoes  — every message the owner sends from the phone's own app (invisible to us
                          otherwise, so the conversation has a hole);
      • history         — up to 180 days of past messages the customer chose to share, once;
      • state_sync      — the business's contacts, at onboarding and on every change.

    The bus is at-most-once (a pod that restarts through one hears it never), and these consumers
    reconcile via a catch-up read, not the raw webhook — so each item is ALSO appended here and read
    forward from a watermark, the same reconciliation path as InboundStore. Idempotent by the item's
    stable id (Meta re-delivers on any non-2xx), so a re-delivery overwrites its row, never duplicates.

    Verified against Meta's coexistence webhook payloads 2026-09-08: smb_message_echoes.message_echoes[]
    ({from,to,id,timestamp,type,...}); history[].threads[].messages[] (each with a stable id);
    smb_app_state_sync.state_sync[] ({type:'contact', contact:{phone_number,full_name,first_name},
    action:'add'|'remove', metadata.timestamp})."""

    # (collection, kind) of every coexistence log — one place, so purge/erase iterate them uniformly.
    _LOGS = ((_ECHOES, _ECHO_KIND), (_HISTORY, _HISTORY_KIND), (_CONTACTS, _CONTACT_KIND))

    def __init__(self, store: DataStore, *, clock=time.time, retention_seconds: int = 0) -> None:
        # retention_seconds<=0 ⇒ no automatic expiry (expires_at=0). The platform sets it (default 30d)
        # so we never store a shop's conversations forever; expires_at is stamped at RECEIPT time (not
        # the message timestamp — a 180-day history import is kept for the retention window from now).
        self._store = store
        self._clock = clock
        self._retention = int(retention_seconds)

    def _expires_at(self) -> float:
        return (self._clock() + self._retention) if self._retention > 0 else 0.0

    async def record_echo(self, *, message_id: str, timestamp: float, tenant: str, contact: str,
                          phone_number_id: str, waba_id: str, message: dict) -> bool:
        """One echoed (owner-sent) message, idempotent by its wamid. `contact` is the WhatsApp user it
        was sent TO (stored so an erasure can find it by subject). False when it has no id."""
        if not message_id:
            return False
        await self._store.put(_ECHOES, message_id, {
            "id": message_id, "kind": _ECHO_KIND, "wamid": message_id, "timestamp": timestamp,
            "contact": contact, "tenant": tenant, "phone_number_id": phone_number_id,
            "waba_id": waba_id, "message": message, "expires_at": self._expires_at()})
        return True

    async def record_history_message(self, *, message_id: str, timestamp: float, contact: str,
                                     tenant: str, phone_number_id: str, waba_id: str,
                                     message: dict) -> bool:
        """One historical message (either direction), idempotent by its wamid. `contact` is the thread's
        WhatsApp user. False when it has no id."""
        if not message_id:
            return False
        await self._store.put(_HISTORY, message_id, {
            "id": message_id, "kind": _HISTORY_KIND, "wamid": message_id, "timestamp": timestamp,
            "contact": contact, "tenant": tenant, "phone_number_id": phone_number_id,
            "waba_id": waba_id, "message": message, "expires_at": self._expires_at()})
        return True

    async def record_contact(self, *, phone_number: str, timestamp: float, action: str, tenant: str,
                             phone_number_id: str, waba_id: str, contact: dict) -> bool:
        """One contact's current state (a sync, so keyed by the contact number PER TENANT — the latest
        add/remove wins; two shops' lists never collide on the same customer). False when no number."""
        if not phone_number:
            return False
        key = f"{len(tenant)}:{tenant}|{phone_number}"
        await self._store.put(_CONTACTS, key, {
            "id": key, "kind": _CONTACT_KIND, "phone_number": phone_number, "timestamp": timestamp,
            "action": action, "tenant": tenant, "phone_number_id": phone_number_id,
            "waba_id": waba_id, "contact": contact, "expires_at": self._expires_at()})
        return True

    async def echoes_since(self, *, after: str | None, limit: int) -> tuple[list[dict], str | None]:
        return await self._store.find(_ECHOES, {"kind": _ECHO_KIND}, limit=limit, after=after,
                                      sort=("timestamp", "asc"))

    async def history_since(self, *, after: str | None, limit: int) -> tuple[list[dict], str | None]:
        return await self._store.find(_HISTORY, {"kind": _HISTORY_KIND}, limit=limit, after=after,
                                      sort=("timestamp", "asc"))

    async def contacts_since(self, *, after: str | None, limit: int) -> tuple[list[dict], str | None]:
        return await self._store.find(_CONTACTS, {"kind": _CONTACT_KIND}, limit=limit, after=after,
                                      sort=("timestamp", "asc"))

    async def purge_expired(self, *, now: float | None = None, limit: int = 1000) -> int:
        """Delete rows whose retention window has passed, across all three logs — so we never keep a
        shop's conversations forever. Ordered by expires_at ascending and stopped at the first row still
        in-window (0 = never-expiring, sorted last), so this is an indexed read, not a full scan. Bounded
        per call; the scheduler re-invokes until a call deletes nothing. Returns how many it removed."""
        cutoff = self._clock() if now is None else now
        removed = 0
        for coll, kind in self._LOGS:
            rows, _ = await self._store.find(coll, {"kind": kind}, limit=limit,
                                             sort=("expires_at", "asc"))
            for r in rows:
                exp = float(r.get("expires_at", 0) or 0)
                if exp <= 0 or exp > cutoff:
                    break                       # never-expiring or still in-window ⇒ nothing older left
                await self._store.delete(coll, r["id"])
                removed += 1
        return removed

    async def erase_subject(self, *, tenant: str, subject: str) -> int:
        """Delete every coexistence row about `subject` (a customer's number) for `tenant` — echoes to
        them, their shared history, their contact entry. For the compliance erasure flow. Returns the
        count deleted; idempotent (a re-run finds nothing)."""
        removed = 0
        for coll, _kind in ((_ECHOES, _ECHO_KIND), (_HISTORY, _HISTORY_KIND)):
            cursor = None
            while True:
                rows, cursor = await self._store.find(
                    coll, {"tenant": tenant, "contact": subject}, limit=500, after=cursor,
                    sort=("timestamp", "asc"))
                for r in rows:
                    await self._store.delete(coll, r["id"])
                    removed += 1
                if not cursor:
                    break
        key = f"{len(tenant)}:{tenant}|{subject}"
        if await self._store.get(_CONTACTS, key) is not None:
            await self._store.delete(_CONTACTS, key)
            removed += 1
        return removed


class WhatsAppNumberIndex:
    """phone_number_id → tenant. A routing map, NOT a secret: a number id is not sensitive, and this
    is what lets a caller holding a shop's WhatsApp NUMBER reach its deliveries without knowing the
    internal tenant id (and, later, attribute an inbound event that arrives keyed only by number).
    Written wherever a number is bound to a tenant — Embedded Signup exchange and registration."""

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def bind(self, phone_number_id: str, tenant: str) -> None:
        # Best-effort and idempotent: a re-registration of the same number rebinds to the same tenant.
        if not phone_number_id or not tenant:
            return
        # id == the key (the phone_number_id), so the row satisfies the backup key==id invariant and
        # this routing map survives a restore rather than silently emptying (and breaking inbound
        # attribution) until every shop happens to re-register.
        await self._store.put(_WA_NUMBER_INDEX, phone_number_id,
                              {"id": phone_number_id, "phone_number_id": phone_number_id,
                               "tenant": tenant})

    async def tenant_of(self, phone_number_id: str) -> str | None:
        if not phone_number_id:
            return None
        row = await self._store.get(_WA_NUMBER_INDEX, phone_number_id)
        return row.get("tenant") if row else None


class InAppStore:
    def __init__(self, store: DataStore, page_size: int = 100, scan_limit: int = 1000) -> None:
        self._store = store
        self._page_size = page_size
        self._scan_limit = scan_limit

    async def add(self, item: InAppItem) -> InAppItem:
        await self._store.put(_INAPP, item.id, item.to_dict())
        return item

    async def feed(self, user: str, limit: int | None = None) -> list[dict]:
        limit = limit or self._page_size
        # One user's feed. `FEED_SCAN_LIMIT` is now the window this read considers - the most
        # recent N of THIS user's items - rather than the number of everybody's items it had to
        # read through to find them.
        rows, _ = await self._store.find(_INAPP, {"user": user}, limit=self._scan_limit,
                                         sort=("ts", "desc"))
        # stable two-pass: newest first, then unread first (ISO timestamps sort lexically)
        rows.sort(key=lambda r: r.get("ts", ""), reverse=True)
        rows.sort(key=lambda r: bool(r.get("read", False)))
        return rows[:limit]

    async def mark_read(self, user: str, item_id: str) -> bool:
        row = await self._store.get(_INAPP, item_id)
        if not row or row.get("user") != user:
            return False
        row["read"] = True
        await self._store.put(_INAPP, item_id, row)
        return True
