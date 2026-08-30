"""Repositories over the generic DataStore port (memory today, postgres by config)."""
from __future__ import annotations

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
_WA_COST = "wa_cost"           # per-message WhatsApp billable-usage unit, keyed by wamid (idempotent)
_WA_COST_KIND = "wa_cost"      # constant kind marker (same role as the other logs)


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
    async def idempotent_get(self, key: str, *, now: float, ttl: int) -> dict | None:
        row = await self._store.get(_IDEMPO, key)
        if not row:
            return None
        if ttl and now and (now - float(row.get("ts", 0) or 0)) > ttl:
            return None          # window elapsed → treat as a fresh send (bounded memory)
        return row.get("result")

    async def remember_idempotent(self, key: str, result: dict, now: float) -> None:
        await self._store.put(_IDEMPO, key, {"result": result, "ts": now})


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


def whatsapp_cost_period(ts: float) -> str:
    """The billing PERIOD a send falls in — a UTC calendar month, YYYY-MM. A month is the unit Meta
    and every downstream rate card meter on; deriving it from the receipt timestamp (not 'now') keeps
    a late-arriving webhook counted in the month the message actually went out."""
    import datetime as _dt
    when = _dt.datetime.fromtimestamp(ts if ts and ts > 0 else _dt.datetime.now(
        _dt.timezone.utc).timestamp(), _dt.timezone.utc)
    return when.strftime("%Y-%m")


class WhatsAppCostStore:
    """Per-message billable-usage units for PROVIDER-BILLED WhatsApp sends, so operations-service can
    read a tenant's real WhatsApp-channel usage for a period and apply its own rate card.

    We record the observed UNITS, never a monetary amount: category (marketing/utility/authentication/
    service), the recipient's country calling code, the pricing_model (PMP/CBP — surfaced verbatim so a
    future Meta billing-model change is visible, not silently read as one model), the billable flag, and
    the message type. The halalas conversion — Meta's per-country/per-category rate card — lives entirely
    on the operations side; embedding it here would BE the pricing decision that must stay in one place.

    Keyed by the wamid, so recording is IDEMPOTENT: a message emits several status events (sent /
    delivered / read) and Meta retries each, but only the one carrying the pricing object records a
    unit, and its wamid key collapses any retry to a single row — a message is counted ONCE. A send we
    cannot tie to a provider-billed delivery is never recorded at all (a Meta-direct send is the shop's
    own bill with Meta, not ours to meter).
    """

    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def record(self, *, wamid: str, tenant: str, ts: float, category: str, country: str,
                     pricing_model: str, billable: bool, msg_type: str) -> bool:
        """Append (idempotently, by wamid) one usage unit. Returns False for a unit with no wamid —
        nothing to key or dedupe on — or one already recorded (first pricing wins, retries are no-ops)."""
        if not wamid:
            return False
        if await self._store.get(_WA_COST, wamid) is not None:
            return False                       # already metered — a webhook retry, not a new message
        await self._store.put(_WA_COST, wamid, {
            "id": wamid, "kind": _WA_COST_KIND, "wamid": wamid, "tenant": tenant,
            "period": whatsapp_cost_period(ts), "category": category, "country": country,
            "pricing_model": pricing_model, "billable": bool(billable), "type": msg_type,
            "created": ts if ts and ts > 0 else 0.0})
        return True

    async def usage_for_period(self, tenant: str, period: str, *, page: int = 500,
                               max_rows: int = 200_000) -> dict:
        """Aggregate ONE tenant's units for ONE period into counts by (category, country,
        pricing_model, billable). A FILTERED read (equality on tenant + period), never a cross-tenant
        scan — keyset-paginated so a busy month is read in bounded pages, with `truncated` set honestly
        if it exceeds the safety cap rather than silently under-counting."""
        empty = {"tenant": tenant, "period": period, "total_messages": 0, "billable_messages": 0,
                 "breakdown": [], "truncated": False}
        if not tenant or not period:
            return empty
        counts: dict[tuple, int] = {}
        total = billable_total = 0
        truncated = False
        cursor: str | None = None
        where = {"tenant": tenant, "period": period}
        while True:
            rows, cursor = await self._store.find(
                _WA_COST, where, limit=page, after=cursor, sort=("created", "asc"))
            for r in rows:
                key = (r.get("category", ""), r.get("country", ""),
                       r.get("pricing_model", ""), bool(r.get("billable")))
                counts[key] = counts.get(key, 0) + 1
                total += 1
                if r.get("billable"):
                    billable_total += 1
                if total >= max_rows:
                    truncated = True
                    break
            if truncated or not cursor or not rows:
                break
        breakdown = [{"category": c, "country": co, "pricing_model": pm, "billable": b, "messages": n}
                     for (c, co, pm, b), n in sorted(counts.items())]
        return {"tenant": tenant, "period": period, "total_messages": total,
                "billable_messages": billable_total, "breakdown": breakdown, "truncated": truncated}


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
