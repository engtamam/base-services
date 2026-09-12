"""Repositories over the generic DataStore — invoices (PIH/ICV chain) + sealed settings."""
from __future__ import annotations

import datetime as _dt
from typing import Any

from app.core.chain import GENESIS_PIH
from app.core.models import ZatcaInvoice, new_id
from app.core.seal import seal, unseal
from app.ports.datastore import DataStore

_INVOICES = "zatca_invoices"
_SETTINGS = "zatca_settings"
_INDEX = "zatca_index"           # head::<tenant> and num::<tenant>::<number> — O(1) lookups

_PAGE = 500
_TENANT_MAX = 100_000     # a bounded read of ONE taxpayer's invoices


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


class InvoiceStore:
    def __init__(self, store: DataStore) -> None:
        self._store = store

    async def save(self, inv: ZatcaInvoice) -> ZatcaInvoice:
        await self._store.put(_INVOICES, inv.id, inv.to_dict())
        return inv

    async def get(self, invoice_id: str) -> ZatcaInvoice | None:
        row = await self._store.get(_INVOICES, invoice_id)
        return ZatcaInvoice.from_dict(row) if row else None

    async def _rows(self, tenant: str, limit: int = _TENANT_MAX) -> list[dict]:
        """One taxpayer's invoices. Never another taxpayer's."""
        out: list[dict] = []
        cursor = None
        while len(out) < limit:
            rows, cursor = await self._store.find(_INVOICES, {"tenant": tenant}, limit=_PAGE,
                                                  after=cursor, sort=("created", "asc"))
            out.extend(rows)
            if not cursor:
                break
        return out[:limit]

    async def by_number(self, tenant: str, number: str) -> ZatcaInvoice | None:
        idx = await self._store.get(_INDEX, f"num::{tenant}::{number}")   # O(1)
        return await self.get(idx["id"]) if idx else None

    async def signed_head(self, tenant: str) -> tuple[int, str]:
        """The CONTIGUOUSLY-signed chain head: the highest ICV whose invoice (and every one before it)
        is signed, and that invoice's hash — which is the PIH the NEXT invoice must carry. Distinct
        from the allocation head (`head::`), which only tracks the highest RESERVED ICV."""
        row = await self._store.get(_INDEX, f"signed::{tenant}")
        return (int(row.get("signed_icv", 0)), row.get("last_hash", "")) if row else (0, "")

    async def mark_signed(self, inv: ZatcaInvoice) -> bool:
        """Persist a signed invoice and advance the signed head — but ONLY contiguously (icv ==
        signed_icv + 1). Compare-and-set so two concurrent signs cannot both advance the head off the
        same base. Returns False if this invoice is not the next link (its predecessor is not signed
        yet); the caller must not treat it as chained. See zatca-01: the PIH/ICV chain forks when an
        invoice is signed while an earlier ICV is still unsigned, so the head advances in strict order."""
        await self.save(inv)
        for _ in range(64):
            row = await self._store.get(_INDEX, f"signed::{inv.tenant}")
            signed_icv = int(row.get("signed_icv", 0)) if row else 0
            ver = int(row.get("version", 0)) if row else 0
            if int(inv.icv) != signed_icv + 1:
                return False                        # not the next link — do not advance out of order
            if await self._store.cas(_INDEX, f"signed::{inv.tenant}", ver,
                                     {"signed_icv": int(inv.icv), "last_hash": inv.hash}):
                return True
        return False

    async def list(self, tenant: str, *, needs_attention: bool = False, limit: int = 100) -> list[dict]:
        rows = await self._rows(tenant)
        if needs_attention:
            rows = [r for r in rows if r.get("status") in ("pending", "signed", "submitted", "rejected")]
        rows.sort(key=lambda r: int(r.get("icv", 0)), reverse=True)
        return [ZatcaInvoice.from_dict(r).public() for r in rows[:limit]]

    # The statuses a UI filters on. A whitelist: the query goes into the datastore, so anything
    # not listed here would let a caller probe rows by a field that is not theirs to query.
    BROWSABLE_STATUS = ("none", "pending", "signed", "submitted", "cleared", "reported",
                        "rejected", "failed")
    ATTENTION_STATUS = ("pending", "signed", "submitted", "rejected")

    async def browse(self, tenant: str, *, status: str = "", needs_attention: bool = False,
                     limit: int = 100, after: str | None = None) -> tuple[list[dict], str | None]:
        """A page of one taxpayer's invoices, narrowed and resumable.

        `list()` reads the taxpayer's WHOLE invoice history into memory and filters in Python. That
        is fine for a sweeper and wrong for a screen: a taxpayer with three years of invoices pays
        for all of them every time somebody opens the page, and there was no cursor, so nothing
        past the cap was reachable or even known to exist.

        A single status is pushed into the datastore. `needs_attention` covers four statuses at
        once, which an equality filter cannot express, so it stays a post-filter over a bounded
        page — honest about what it is rather than pretending to be indexed.
        """
        if status and status not in self.BROWSABLE_STATUS:
            raise ValueError(f"UNKNOWN_STATUS:{status}")
        query: dict = {"tenant": tenant}
        if status:
            query["status"] = status
        rows, cursor = await self._store.find(_INVOICES, query, limit=limit, after=after,
                                              sort=("created", "desc"))
        if needs_attention:
            rows = [r for r in rows if r.get("status") in self.ATTENTION_STATUS]
        return [ZatcaInvoice.from_dict(r).public() for r in rows], cursor

    async def due_for_retry(self, now_ts: float, limit: int = 100) -> list[ZatcaInvoice]:
        """Invoices not yet cleared by ZATCA and ready for another attempt.

        The three unfinished statuses are pushed down, so the resubmission sweeper reads the
        backlog rather than every invoice the taxpayer has ever issued. `next_retry_at` is a
        time comparison and stays here.
        """
        out: list[ZatcaInvoice] = []
        cursor = None
        while len(out) < limit:
            # Ordered by next_retry_at ASC (indexed) so DUE invoices come first and we STOP at the
            # first one whose retry time is still in the future — O(due), not O(all unfinished). This
            # relies on capped invoices being moved to a terminal status (see _schedule_retry), so the
            # swept set never accumulates never-due rows. A row with no next_retry_at is transient
            # (created, not yet attempted); skip it without stopping the scan.
            rows, cursor = await self._store.find(
                _INVOICES, {"status": ["pending", "signed", "submitted"]},
                limit=_PAGE, after=cursor, sort=("next_retry_at", "asc"))
            stop = False
            for r in rows:
                nra = r.get("next_retry_at")
                if nra is None:
                    continue                        # transient, not yet scheduled — keep scanning
                if nra > now_ts:
                    stop = True                     # ascending: everything after this is also future
                    break
                out.append(ZatcaInvoice.from_dict(r))
                if len(out) >= limit:
                    break
            if stop or not cursor:
                break
        return out[:limit]

    async def chain_links(self, tenant: str) -> list[dict]:
        return [{"icv": r.get("icv"), "pih": r.get("pih"), "hash": r.get("hash"),
                 "status": r.get("status")} for r in await self._rows(tenant)]

    async def allocate_slot(self, tenant: str, number: str, invoice_type: str) -> tuple[ZatcaInvoice, bool]:
        """Idempotent by (tenant, number). Reserve the next ICV + PIH (= prior invoice's hash).

        ATOMIC ICV reservation: the head pointer alone was a non-atomic read-modify-write, so two
        concurrent finalizes for one tenant both read reserved=N and both minted ICV N+1 — duplicate
        ICVs that corrupt the PIH/ICV chain. Now each ICV is claimed by an atomic put-if-absent on a
        unique `icv::{tenant}::{icv}` key; a loser (key taken) re-reads and tries the next ICV. The
        head pointer stays only as an O(1) hint for where to start — the icv-claim is the authority."""
        existing = await self.by_number(tenant, number)
        if existing is not None:
            return existing, False
        head = await self._store.get(_INDEX, f"head::{tenant}") or {"reserved_icv": 0, "last_hash": ""}
        icv = int(head.get("reserved_icv", 0)) + 1
        for _ in range(10_000):                              # bounded; advances past ICVs already taken
            claimed = await self._store.create(_INDEX, f"icv::{tenant}::{icv}", {"number": str(number)})
            if claimed:
                break
            icv += 1                                         # this ICV was taken by a concurrent finalize
        else:
            raise RuntimeError("could not reserve an ICV slot")
        # PROVISIONAL PIH: a best-effort hint from the signed head at allocation time. The AUTHORITATIVE
        # PIH is bound at SIGN time (zatca-01), when the predecessor is actually signed — because
        # signing is deferred/retryable and out of order, the last-signed hash known now may not be the
        # true predecessor's hash by the time this invoice signs.
        _, signed_hash = await self.signed_head(tenant)
        pih = signed_hash if (icv > 1 and signed_hash) else GENESIS_PIH
        inv = ZatcaInvoice(id=new_id(), tenant=tenant, number=str(number), icv=icv,
                           pih=pih, status="pending", invoice_type=invoice_type, created=_now())
        await self.save(inv)
        await self._store.put(_INDEX, f"num::{tenant}::{number}", {"id": inv.id})
        # Advance the head HINT if we moved it forward — re-read so we never clobber a concurrent
        # sign's last_hash (the icv-claim, not this hint, is the uniqueness authority).
        cur = await self._store.get(_INDEX, f"head::{tenant}") or {"reserved_icv": 0, "last_hash": ""}
        if icv > int(cur.get("reserved_icv", 0)):
            cur["reserved_icv"] = icv
            await self._store.put(_INDEX, f"head::{tenant}", cur)
        return inv, True


class SettingsStore:
    """Per-tenant ZATCA settings + SEALED CSID material (never returned in views)."""

    def __init__(self, store: DataStore, seal_key: str) -> None:
        self._store = store
        self._seal_key = seal_key

    async def _raw(self, tenant: str) -> dict:
        return await self._store.get(_SETTINGS, tenant) or {}

    async def view(self, tenant: str) -> dict[str, Any]:
        r = await self._raw(tenant)
        return {
            "tenant": tenant, "enabled": bool(r.get("enabled", False)),
            "environment": r.get("environment", "sandbox"),
            "seller_name": r.get("seller_name", ""), "seller_vat": r.get("seller_vat", ""),
            "seller_address": r.get("seller_address", ""),
            "onboarding_status": r.get("onboarding_status", "none"),
            "onboarded": bool(r.get("csid_cert_enc") and r.get("private_key_enc")),
        }

    async def put(self, tenant: str, patch: dict[str, Any]) -> dict[str, Any]:
        r = await self._raw(tenant)
        for k in ("enabled", "environment", "seller_name", "seller_vat", "seller_address",
                  "onboarding_status"):
            if k in patch and patch[k] is not None:
                r[k] = patch[k]
        r["id"] = tenant        # carry the key as the row id so a backup restores under it
        await self._store.put(_SETTINGS, tenant, r)
        return await self.view(tenant)

    async def store_csid(self, tenant: str, *, certificate_pem: str, secret: str,
                         private_key_pem: str | None = None, onboarding_status: str = "production",
                         request_id: str | None = None) -> None:
        r = await self._raw(tenant)
        r["csid_cert_enc"] = seal(certificate_pem, self._seal_key)
        r["csid_secret_enc"] = seal(secret, self._seal_key)
        if private_key_pem is not None:            # production exchange keeps the compliance key
            r["private_key_enc"] = seal(private_key_pem, self._seal_key)
        if request_id is not None:
            r["compliance_request_id"] = request_id
        r["onboarding_status"] = onboarding_status
        r["id"] = tenant        # carry the key as the row id so a backup restores under it
        await self._store.put(_SETTINGS, tenant, r)

    async def request_id(self, tenant: str) -> str:
        return (await self._raw(tenant)).get("compliance_request_id", "")

    async def load_secrets(self, tenant: str) -> dict[str, str] | None:
        r = await self._raw(tenant)
        cert = unseal(r.get("csid_cert_enc", ""), self._seal_key)
        key = unseal(r.get("private_key_enc", ""), self._seal_key)
        if not (cert and key):
            return None
        import base64
        return {"certificate_pem": cert, "private_key_pem": key,
                "secret": unseal(r.get("csid_secret_enc", ""), self._seal_key) or "",
                "cert_base64": base64.b64encode(cert.encode()).decode()}
