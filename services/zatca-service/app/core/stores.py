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

    async def record_hash(self, inv: ZatcaInvoice) -> None:
        """Persist a signed invoice AND advance the chain head (last_hash) — O(1)."""
        await self.save(inv)
        head = await self._store.get(_INDEX, f"head::{inv.tenant}") or {"reserved_icv": 0, "last_hash": ""}
        if int(inv.icv) >= int(head.get("reserved_icv", 0)):
            head["reserved_icv"] = max(int(head.get("reserved_icv", 0)), int(inv.icv))
            head["last_hash"] = inv.hash
            await self._store.put(_INDEX, f"head::{inv.tenant}", head)

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
            rows, cursor = await self._store.find(
                _INVOICES, {"status": ["pending", "signed", "submitted"]},
                limit=_PAGE, after=cursor, sort=("created", "asc"))
            for r in rows:
                nra = r.get("next_retry_at")
                if nra is not None and nra <= now_ts:
                    out.append(ZatcaInvoice.from_dict(r))
            if not cursor:
                break
        return out[:limit]

    async def chain_links(self, tenant: str) -> list[dict]:
        return [{"icv": r.get("icv"), "pih": r.get("pih"), "hash": r.get("hash"),
                 "status": r.get("status")} for r in await self._rows(tenant)]

    async def allocate_slot(self, tenant: str, number: str, invoice_type: str) -> tuple[ZatcaInvoice, bool]:
        """Idempotent by (tenant, number). Reserve the next ICV + PIH (= prior invoice's hash).
        O(1) via the per-tenant head pointer + number index (no full scan)."""
        existing = await self.by_number(tenant, number)
        if existing is not None:
            return existing, False
        head = await self._store.get(_INDEX, f"head::{tenant}") or {"reserved_icv": 0, "last_hash": ""}
        reserved = int(head.get("reserved_icv", 0))
        icv = reserved + 1
        pih = head["last_hash"] if (reserved > 0 and head.get("last_hash")) else GENESIS_PIH
        inv = ZatcaInvoice(id=new_id(), tenant=tenant, number=str(number), icv=icv,
                           pih=pih, status="pending", invoice_type=invoice_type, created=_now())
        await self.save(inv)
        await self._store.put(_INDEX, f"num::{tenant}::{number}", {"id": inv.id})
        head["reserved_icv"] = icv                        # reserve the ICV (last_hash set on sign)
        await self._store.put(_INDEX, f"head::{tenant}", head)
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
