"""Repositories over the generic DataStore — invoices (PIH/ICV chain) + sealed settings."""
from __future__ import annotations

import asyncio
import datetime as _dt
import time as _time
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
# A `num::` claim whose invoice row never appeared and that is older than this is a crash orphan (a
# real allocate_slot completes in well under a second), safe for allocate_slot to free and reuse.
_STALE_NUM_CLAIM_S = 60.0


def _csid_aad(tenant: str, field: str) -> str:
    """Associated data binding a sealed CSID blob to its (collection|tenant|field) slot, so a
    tampered DB cannot relocate it across tenants or swap it between fields (see seal.py)."""
    return f"{_SETTINGS}|{tenant}|{field}"


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
        """Persist the signed body durably FIRST, then advance the signed head CONTIGUOUSLY
        (icv == signed_icv + 1). Returns True iff THIS invoice is durably signed AND the chain head
        now reflects it. False means "not the next link / not yet committed" — do not submit.

        Two invariants that pull in opposite directions, both satisfied here:

        Atomicity (zatca-002). The old order advanced the head CAS and only THEN called save(inv):
        two non-atomic ops. A crash in between left the head reporting ICV N as signed with hash H
        while row N was still status='pending', hash='' — and because ZATCA signing is
        NON-deterministic (a fresh SigningTime each time), a re-sign could never reproduce H, so N
        was unsignable and unsubmittable forever and every successor deferred behind it. So the body
        is now persisted BEFORE the head advances: a crash leaves a fully-signed, self-consistent row
        and a head merely one behind, which the next sign re-drives with NO re-signing (it adopts the
        durable bytes and just advances the head).

        Byte-safety (zatca-r2-02). A losing concurrent sign of the same ICV must never overwrite the
        winner's already-stored signed_xml/hash with its own non-deterministic bytes. The single body
        writer is elected write-once via an insert-if-absent claim (`signedbody::`); a loser (or a
        retry) whose row already carries a signed body ADOPTS those durable bytes and never writes.
        The claim is crash-reclaimable on the same staleness rule allocate_slot uses for `num::`
        orphans, so a writer that died before persisting the body does not wedge the ICV either.
        """
        body_claim = f"signedbody::{inv.tenant}::{inv.icv}"
        # ── 1) durably persist the signed body exactly once (crash-reclaimable) ──────────────────
        while True:
            durable = await self.get(inv.id)
            if durable is not None and durable.signed_xml:
                # A winner (or our own earlier, pre-crash attempt) already stored the body. Adopt the
                # DURABLE bytes/hash so the caller submits the authoritative document and the head is
                # advanced to the hash that actually exists — never our losing re-signed bytes.
                inv.signed_xml, inv.hash = durable.signed_xml, durable.hash
                inv.qr, inv.status = durable.qr, (durable.status or "signed")
                break
            claim = await self._store.get(_INDEX, body_claim)
            if claim is None:
                if await self._store.create(_INDEX, body_claim,
                                            {"invoice_id": inv.id, "at": _time.time()}):
                    await self.save(inv)            # sole elected writer — persist immutable bytes
                    break
                continue                            # lost the create race — re-read and adopt
            if _time.time() - float(claim.get("at", 0)) < _STALE_NUM_CLAIM_S:
                return False                        # an in-flight winner is persisting — retry later
            # A stale claim whose body never landed = a crashed writer. Reclaim and write ours.
            await self._store.delete(_INDEX, body_claim)
        # ── 2) advance the signed head to this durably-signed body, contiguously + idempotently ──
        for _ in range(64):
            row = await self._store.get(_INDEX, f"signed::{inv.tenant}")
            signed_icv = int(row.get("signed_icv", 0)) if row else 0
            ver = int(row.get("version", 0)) if row else 0
            if signed_icv >= int(inv.icv):
                # The head already reflects this ICV — someone else won the advance and OWNS the
                # submit for it (or a prior sweep already submitted). Returning False here is what
                # keeps a losing concurrent sign from ALSO submitting the same invoice (zatca-r2-02):
                # the invoice is durably signed, so a later sweep drives submission if the winner
                # died before it. Only the caller that actually advances the head submits.
                return False
            if int(inv.icv) != signed_icv + 1:
                return False                        # predecessor not signed yet — do not skip ahead
            if await self._store.cas(_INDEX, f"signed::{inv.tenant}", ver,
                                     {"signed_icv": int(inv.icv), "last_hash": inv.hash}):
                return True                         # WE advanced the head — WE own the submit
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
        head pointer stays only as an O(1) hint for where to start — the icv-claim is the authority.

        ATOMIC number idempotency (zatca-r2-02 / r2-01): the number claim `num::{tenant}::{number}` is
        the authority for one-ICV-per-number and is written FIRST via put-if-absent, BEFORE any ICV is
        minted. The old code read `by_number()` then plainly `put` the num:: row, so two concurrent
        finalizes for the SAME number both saw None, both minted a (distinct) ICV, and both saved an
        invoice row — a second orphan row the sweep later signed and filed under a duplicate ICV. Now
        exactly one caller wins the create(); the loser re-reads and returns the winner's invoice
        WITHOUT ever claiming an ICV."""
        number = str(number)
        # Claim the number FIRST. The loser of a same-number race never reaches the ICV mint below.
        # A crash BETWEEN this claim and save() (before any icv:: claim, so reconcile_orphans — which
        # walks icv:: — never sees it) would otherwise wedge this number FOREVER: every later allocate
        # loses the create, finds no invoice, and raises a 500. The bounded retry below frees such a
        # STALE orphan and reuses the number, while never touching a genuinely in-flight claim.
        inv_id = ""
        for _claim_try in range(3):
            existing = await self.by_number(tenant, number)
            if existing is not None:
                return existing, False
            candidate = new_id()
            if await self._store.create(_INDEX, f"num::{tenant}::{number}",
                                        {"id": candidate, "claimed_ts": _time.time()}):
                inv_id = candidate
                break                                        # won the number claim
            row = await self._store.get(_INDEX, f"num::{tenant}::{number}")
            for _ in range(10_000):                          # a concurrent winner is mid-flight; wait for
                existing = await self.by_number(tenant, number)   # its invoice row, then return it
                if existing is not None:
                    return existing, False
                await asyncio.sleep(0)                        # yield so the winner's save() can land
            # No invoice appeared. Free it and retry ONLY when the claim is old enough to be a crash
            # orphan (a live winner finishes in << _STALE_NUM_CLAIM_S) AND its id resolves to no
            # invoice — so an in-flight claim of the same instant is never stolen. A legacy claim with
            # no claimed_ts (ts 0) reads as stale, healing orphans written before this fix.
            if (row is not None
                    and _time.time() - float(row.get("claimed_ts", 0) or 0) >= _STALE_NUM_CLAIM_S
                    and not await self.get(row.get("id", ""))):
                await self._store.delete(_INDEX, f"num::{tenant}::{number}")
                continue                                     # orphan freed — retry the claim
            raise RuntimeError("number claimed but invoice row never appeared")
        else:
            raise RuntimeError("number claim contended repeatedly")
        head = await self._store.get(_INDEX, f"head::{tenant}") or {"reserved_icv": 0, "last_hash": ""}
        icv = int(head.get("reserved_icv", 0)) + 1
        for _ in range(10_000):                              # bounded; advances past ICVs already taken
            claimed = await self._store.create(_INDEX, f"icv::{tenant}::{icv}", {"number": number})
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
        inv = ZatcaInvoice(id=inv_id, tenant=tenant, number=number, icv=icv,
                           pih=pih, status="pending", invoice_type=invoice_type, created=_now())
        await self.save(inv)
        # Advance the head HINT if we moved it forward — re-read so we never clobber a concurrent
        # sign's last_hash (the icv-claim, not this hint, is the uniqueness authority).
        cur = await self._store.get(_INDEX, f"head::{tenant}") or {"reserved_icv": 0, "last_hash": ""}
        if icv > int(cur.get("reserved_icv", 0)):
            cur["reserved_icv"] = icv
            # cas-create: ok — this put writes the `head::` (reserved-hint) key, which is never
            # version-pinned cas-updated. The version-pinned cas in this file is on the SEPARATE
            # `signed::` key, which mark_signed CREATES via cas(ver=0-when-absent) insert-if-absent —
            # so no row is ever put-created then cas-updated on a versionless row (the flagged class).
            await self._store.put(_INDEX, f"head::{tenant}", cur)
        return inv, True

    async def reserved_head(self, tenant: str) -> int:
        """The highest ICV ever RESERVED for the tenant (the allocation head hint)."""
        row = await self._store.get(_INDEX, f"head::{tenant}")
        return int(row.get("reserved_icv", 0)) if row else 0

    async def head_lag(self, tenant: str) -> dict[str, int]:
        """How far the RESERVED head leads the contiguously-SIGNED head.

        A permanently-unsignable ICV defers every later one (strict in-order signing), so this gap
        growing is the earliest symptom of a stuck tail (zatca-r2-03). reserved_icv - signed_icv is
        the number of ICVs waiting behind the signed head."""
        reserved = await self.reserved_head(tenant)
        signed_icv, _ = await self.signed_head(tenant)
        return {"reserved_icv": reserved, "signed_icv": signed_icv,
                "lag": max(0, reserved - signed_icv)}

    async def _invoice_at_icv(self, tenant: str, icv: int) -> ZatcaInvoice | None:
        """The invoice row occupying ICV `icv` for the tenant, or None. Equality-scoped read."""
        rows, _ = await self._store.find(_INVOICES, {"tenant": tenant, "icv": icv}, limit=1)
        return ZatcaInvoice.from_dict(rows[0]) if rows else None

    async def reconcile_orphans(self, tenant: str, *, max_scan: int = _TENANT_MAX) -> dict[str, Any]:
        """Free `icv::` claims that no invoice row stands behind (a crash mid-allocate_slot).

        allocate_slot is three non-transactional steps — claim `icv::{tenant}::{n}`, save the
        invoice row, advance the head hint — so a crash after the claim but before the save leaves
        `icv::{tenant}::{n}` (and the `num::` claim written before it) with NO invoice behind them.
        A later finalize probes upward from the head and skips the taken `icv::n`, minting n+1 —
        leaving a PERMANENT gap at n that verify_chain reports as icv_gap forever (zatca-r2-07).

        This walks every reserved ICV, and for any `icv::` claim with no matching invoice row frees
        that claim (and the dangling `num::` claim it names, only when that number's id resolves to
        no invoice) so the next finalize REUSES n. It NEVER touches a claim a real invoice occupies —
        gapless is preserved by reclaiming the hole, never by skipping it."""
        reserved = await self.reserved_head(tenant)
        reclaimed: list[int] = []
        # Walk every reserved ICV, and KEEP walking past the head hint while claims still exist: a
        # crash orphans the claim AFTER minting it but BEFORE the head advances (its last step), so
        # the orphan sits at reserved+1 — above the hint. Stop at the first absent claim beyond the
        # head. Bounded by max_scan so a corrupt hint can never make this unbounded.
        n = 0
        while n < int(max_scan):
            n += 1
            claim = await self._store.get(_INDEX, f"icv::{tenant}::{n}")
            if claim is None:
                if n > reserved:
                    break                                    # past the head, no more claims — done
                continue                                     # a hole below the head (reclaimed/never set)
            if await self._invoice_at_icv(tenant, n) is not None:
                continue                                     # a real invoice occupies it — untouchable
            # Orphan: the claim survived a crash the invoice never did. Free it so n is reused.
            number = claim.get("number")
            await self._store.delete(_INDEX, f"icv::{tenant}::{n}")
            if number is not None:
                num_row = await self._store.get(_INDEX, f"num::{tenant}::{number}")
                # Free the number ONLY when it too is dangling (its id resolves to no invoice) — a
                # number a real invoice owns must never be released.
                if num_row and not await self.get(num_row.get("id", "")):
                    await self._store.delete(_INDEX, f"num::{tenant}::{number}")
            reclaimed.append(n)
        return {"tenant": tenant, "reclaimed": reclaimed, "count": len(reclaimed)}

    async def release_tail_slot(self, inv: ZatcaInvoice, *, max_scan: int = _TENANT_MAX) -> None:
        """Delete a TAIL invoice row and free its `icv::`/`num::` claims so the number is reused.

        Precondition (enforced by the caller): `inv` is the HIGHEST reserved ICV and unsigned. This
        deletes the invoice row, frees `icv::{tenant}::{inv.icv}` and `num::{tenant}::{inv.number}`,
        and ROLLS THE RESERVED HEAD BACK to inv.icv-1 — so the next allocate_slot starts at inv.icv
        and refills the same number. The signed head (`signed::`) is never touched: nothing signed
        chains through a tail that was never signed. Gapless holds — the released number is the
        highest, so no gap opens beneath it."""
        await self._store.delete(_INVOICES, inv.id)
        await self._store.delete(_INDEX, f"icv::{inv.tenant}::{inv.icv}")
        num_row = await self._store.get(_INDEX, f"num::{inv.tenant}::{inv.number}")
        if num_row and num_row.get("id") == inv.id:
            await self._store.delete(_INDEX, f"num::{inv.tenant}::{inv.number}")
        # Roll the reserved-head hint back so the next allocation refills inv.icv rather than
        # probing from inv.icv+1 and re-opening the very gap this closes. Re-read so a concurrent
        # sign's last_hash is preserved (the icv-claim, now freed, was the authority).
        cur = await self._store.get(_INDEX, f"head::{inv.tenant}")
        if cur and int(cur.get("reserved_icv", 0)) >= inv.icv:
            cur["reserved_icv"] = inv.icv - 1
            # atomic: ok — head:: is only an allocation HINT (the icv:: claim is the uniqueness
            # authority); a lost update here at worst probes from a stale point and self-heals via
            # allocate_slot's bounded advance-past-taken loop, never a gap. Same as allocate_slot's put.
            await self._store.put(_INDEX, f"head::{inv.tenant}", cur)

    async def highest_reserved_above(self, tenant: str, icv: int, *, max_scan: int = _TENANT_MAX) -> int:
        """The highest ICV at or above `icv+1` that is still RESERVED (claim or invoice row) — 0 if
        none. Used to prove a slot is the true tail before releasing it: any reservation above means
        a higher ICV would be orphaned by a release, so the release must be refused."""
        reserved = await self.reserved_head(tenant)
        top = 0
        n = icv
        while n < int(max_scan):
            n += 1
            if await self._store.get(_INDEX, f"icv::{tenant}::{n}") is not None:
                top = n
            elif await self._invoice_at_icv(tenant, n) is not None:
                top = n
            elif n > reserved:
                break                                        # past the head, nothing reserved above
        return top


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
        # AAD binds each blob to (collection|tenant|field), so a tampered DB cannot relocate a sealed
        # CSID across tenants or swap it between the cert/secret/key fields — it would fail the tag.
        r["csid_cert_enc"] = seal(certificate_pem, self._seal_key, _csid_aad(tenant, "csid_cert"))
        r["csid_secret_enc"] = seal(secret, self._seal_key, _csid_aad(tenant, "csid_secret"))
        if private_key_pem is not None:            # production exchange keeps the compliance key
            r["private_key_enc"] = seal(private_key_pem, self._seal_key,
                                        _csid_aad(tenant, "private_key"))
        if request_id is not None:
            r["compliance_request_id"] = request_id
        r["onboarding_status"] = onboarding_status
        r["id"] = tenant        # carry the key as the row id so a backup restores under it
        await self._store.put(_SETTINGS, tenant, r)

    async def request_id(self, tenant: str) -> str:
        return (await self._raw(tenant)).get("compliance_request_id", "")

    async def load_secrets(self, tenant: str) -> dict[str, str] | None:
        r = await self._raw(tenant)
        cert = unseal(r.get("csid_cert_enc", ""), self._seal_key, _csid_aad(tenant, "csid_cert"))
        key = unseal(r.get("private_key_enc", ""), self._seal_key, _csid_aad(tenant, "private_key"))
        if not (cert and key):
            return None
        import base64
        return {"certificate_pem": cert, "private_key_pem": key,
                "secret": unseal(r.get("csid_secret_enc", ""), self._seal_key,
                                 _csid_aad(tenant, "csid_secret")) or "",
                "cert_base64": base64.b64encode(cert.encode()).decode()}
