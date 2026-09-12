"""Core ZATCA orchestration — finalize / sweep / resubmit / verify / onboard.

Guarantees (the smart part):
  • fail-closed signing — an unavailable signer NEVER fabricates a signature.
  • non-fatal issuance — finalize never raises to the caller; a transient failure
    leaves the invoice retryable ('pending'/'signed'), never dropped.
  • no ICV wasted — the slot is allocated once (idempotent by number) and reused on
    every retry, so the PIH/ICV chain never gaps.
  • tamper-evident chain — verify walks PIH/ICV from genesis (first-break-wins).
"""
from __future__ import annotations
import asyncio

import time
from typing import Any

from app.core import chain as _chain
from app.core import ubl as _ubl
from app.core.backoff import compute_retry_schedule
from app.core.classify import classify
from app.core.models import ACCEPTED, ZatcaInvoice
from app.core.stores import InvoiceStore, SettingsStore
from app.ports.fatoora_client import FatooraUnavailable
from app.ports.zatca_signer import SignerUnavailable


class ZatcaInputError(ValueError):
    """The invoice payload is malformed (a bad line amount or currency). A PERMANENT caller error the
    route maps to 422 — distinct from a transient signer/gateway failure, which stays retryable. It is
    raised BEFORE an ICV slot is allocated, so a malformed request never burns a chain slot."""


def _validate_invoice_input(invoice_in: dict, default_currency: str) -> None:
    """Fail a malformed payload loudly and early, before any slot is reserved.

    build_ubl() coerces `amount_minor`, `qty` and `issued_at` with int() and rejects a non-3-letter
    currency by raising ValueError mid-build — which, left uncaught, surfaced as an unhandled 500
    that contradicted the 'finalize never fails the caller' guarantee AND burned the ICV that
    allocate_slot had already reserved (zatca-r2-05). Validation must therefore cover EVERY field
    build_ubl int()-coerces, so a payload that passes here is guaranteed to build. It runs before
    allocate_slot, so a bad request never consumes an ICV. A missing `number` (zatca-r2-11) is a
    clean 422 here rather than a KeyError 500 in finalize's by_number lookup.
    """
    if invoice_in.get("number") in (None, ""):
        raise ZatcaInputError("invoice number is required")
    cur = str(invoice_in.get("currency", default_currency) or default_currency).upper()
    if not (len(cur) == 3 and cur.isalpha()):
        raise ZatcaInputError(f"invalid currency code: {invoice_in.get('currency')!r}")
    issued_at = invoice_in.get("issued_at")
    if issued_at is not None:                       # build_ubl: int(inv.get('issued_at') or 0)
        try:
            int(issued_at)
        except (TypeError, ValueError):
            raise ZatcaInputError(f"issued_at is not an integer epoch: {issued_at!r}") from None
    lines = invoice_in.get("lines")
    if not isinstance(lines, list) or not lines:
        raise ZatcaInputError("invoice has no lines")
    for i, ln in enumerate(lines):
        if not isinstance(ln, dict) or "amount_minor" not in ln:
            raise ZatcaInputError(f"line {i}: missing amount_minor")
        try:
            int(ln["amount_minor"])
        except (TypeError, ValueError):
            raise ZatcaInputError(f"line {i}: amount_minor is not an integer") from None
        if "qty" in ln:                            # build_ubl: int(lines[i].get('qty', 1))
            try:
                int(ln["qty"])
            except (TypeError, ValueError):
                raise ZatcaInputError(f"line {i}: qty is not an integer") from None


class ZatcaService:
    def __init__(self, *, invoices: InvoiceStore, settings: SettingsStore, signer, fatoora,
                 qr, vat_rate: str, currency: str, retry: dict[str, int], list_limit: int = 100,
                 validate_before_submit: bool = False, signer_provider=None) -> None:
        self._inv = invoices
        self._set = settings
        self._signer = signer
        # Resolve the signer PER TENANT at sign time. The old design built one signer from the
        # request's tenant (or "") and reused it for every invoice — so a multi-tenant sweep signed
        # every tenant with one token, and a per-tenant ZATCA_SIGNER_TOKEN set by an operator was
        # never consulted. With a provider, each invoice is signed with ITS tenant's credential.
        self._signer_provider = signer_provider
        self._fatoora = fatoora
        self._qr = qr
        self._vat_rate = vat_rate
        self._currency = currency
        self._retry = retry
        self._limit = list_limit
        self._validate = validate_before_submit

    # ── issuance ────────────────────────────────────────────────────────────────
    async def finalize(self, invoice_in: dict, tenant: str) -> dict[str, Any]:
        # Guard `number` presence FIRST: the by_number lookup below indexes on str(number), so a
        # payload without one raised KeyError -> unhandled 500 before any validation (zatca-r2-11).
        if invoice_in.get("number") in (None, ""):
            raise ZatcaInputError("invoice number is required")
        number = str(invoice_in["number"])
        # Validate the payload BEFORE reserving an ICV: a malformed line or currency is a permanent
        # caller error (422), and reserving a slot for one would burn a chain ICV that can never be
        # signed. Only runs on the fresh path — a retry re-enters with the draft already built.
        if not (await self._inv.by_number(tenant, number)):
            _validate_invoice_input(invoice_in, self._currency)
        simplified = not (invoice_in.get("buyer") or {}).get("vat_number")
        itype = "simplified" if simplified else "standard"
        inv, _created = await self._inv.allocate_slot(tenant, number, itype)
        if inv.status in ACCEPTED:
            return self._result(inv)                      # idempotent — already done

        if not inv.draft:                                  # build the unsigned UBL once
            built = _ubl.build_ubl({**invoice_in, "vat_rate": invoice_in.get("vat_rate", self._vat_rate),
                                    "currency": invoice_in.get("currency", self._currency)},
                                   icv=inv.icv, pih=inv.pih, invoice_type=itype)
            inv.uuid = built["uuid"]
            inv.draft = {"unsigned_xml": built["xml"],
                         "seller_name": (invoice_in.get("seller") or {}).get("name", ""),
                         "vat_number": (invoice_in.get("seller") or {}).get("vat_number", ""),
                         "invoice_total": built["tax_inclusive"], "vat_total": built["vat"],
                         "simplified": simplified}
            await self._inv.save(inv)

        secrets = await self._set.load_secrets(tenant)
        if secrets is None:                                # not onboarded → stay pending (fail-soft)
            return await self._defer(inv, "not onboarded (no CSID); staying pending")
        if inv.status in ("none", "pending"):
            if not await self._sign(inv, secrets):
                return self._result(inv)
        return await self._submit(inv, secrets)

    async def _sign(self, inv: ZatcaInvoice, secrets: dict) -> bool:
        # ORDER + PIH binding (zatca-01). Signing is deferred/retryable and can run out of ICV order,
        # so the PIH baked at allocation may point at the wrong predecessor. Here we require the
        # immediately-preceding ICV to be signed, bind THIS invoice's PIH to that predecessor's real
        # hash (rebuilding just the PIH in the unsigned XML), and sign the corrected document. If the
        # predecessor is not signed yet, we defer — the chain is built strictly in order.
        # Byte-safety guard (zatca-r2-02): never re-sign an invoice that a concurrent actor has
        # already signed. A second sign of the same ICV (two overlapping sweeps, or sweep-vs-resubmit)
        # must be a no-op on the stored bytes — the losing sign that reached here would otherwise call
        # the SDK again and hand mark_signed a different hash. If the durable row already carries a
        # signed body (or is terminally accepted), defer to it rather than re-signing. A 'failed' row
        # with no signed body is still re-signable (zatca-r2-06 resubmit recovery).
        fresh = await self._inv.get(inv.id)
        if fresh is not None and (fresh.signed_xml or fresh.status in ACCEPTED):
            return False
        signed_icv, signed_hash = await self._inv.signed_head(inv.tenant)
        if int(inv.icv) != signed_icv + 1:
            await self._defer(inv, f"awaiting ICV {signed_icv + 1} before signing ICV {inv.icv}")
            return False
        correct_pih = signed_hash if inv.icv > 1 else _chain.GENESIS_PIH
        if inv.pih != correct_pih:
            inv.pih = correct_pih
            inv.draft = {**inv.draft, "unsigned_xml": _ubl.set_pih(inv.draft["unsigned_xml"], correct_pih)}
            await self._inv.save(inv)
        d = inv.draft
        # Per-tenant signer: resolve this invoice's tenant credential rather than a request-wide one.
        signer = self._signer
        if self._signer_provider is not None:
            try:
                signer = await self._signer_provider(inv.tenant)
            except Exception:  # noqa: BLE001 — a credential resolution failure must not 500 a filing
                await self._defer(inv, "signer credential unavailable")
                return False
        try:
            signed = await asyncio.to_thread(                 # blocking SDK signer OFF the event loop
                signer.sign,
                invoice_xml=d["unsigned_xml"], certificate_pem=secrets["certificate_pem"],
                private_key_pem=secrets["private_key_pem"], seller_name=d["seller_name"],
                vat_number=d["vat_number"], invoice_total=d["invoice_total"],
                vat_total=d["vat_total"], simplified=d["simplified"])
        except SignerUnavailable as ex:
            await self._defer(inv, str(ex))
            return False
        if not all((signed.get("signedXml"), signed.get("invoiceHash"), signed.get("signatureBase64"))):
            await self._defer(inv, "incomplete signer response")
            return False
        inv.signed_xml = signed["signedXml"]
        inv.hash = signed["invoiceHash"]                   # this hash = next invoice's PIH
        inv.qr = signed.get("qrBase64", "")
        inv.status = "signed"
        # Advance the signed head CONTIGUOUSLY, persisting the signed body ONLY if this invoice wins
        # the head CAS (mark_signed now does head-CAS-then-save). HONOR its return (zatca-r2-02): a
        # losing concurrent sign gets False — its bytes were NOT persisted, so we must NOT submit them.
        # The caller (finalize/sweep/resubmit) treats False as "defer, do not submit".
        return await self._inv.mark_signed(inv)

    def _validate_ok(self, inv: ZatcaInvoice) -> bool:
        """Optional pre-submit check with the official SDK validator. Fail-OPEN if the
        validator is unavailable (don't block a valid invoice); fail-CLOSED on 'invalid'."""
        if not self._validate:
            return True
        try:
            result = self._signer.validate(inv.signed_xml)
        except SignerUnavailable:
            return True
        return str(result.get("status", "")).lower() not in ("invalid", "validator_error")

    async def _submit(self, inv: ZatcaInvoice, secrets: dict) -> dict[str, Any]:
        if not self._validate_ok(inv):                     # SDK says the signed XML is invalid
            inv.status = "rejected"
            inv.message = "pre-submit SDK validation failed"
            inv.next_retry_at = None
            await self._inv.save(inv)
            return self._result(inv)
        call = self._fatoora.clear if inv.invoice_type == "standard" else self._fatoora.report
        success = "cleared" if inv.invoice_type == "standard" else "reported"
        creds = {"token": secrets.get("cert_base64"), "secret": secrets.get("secret")}
        try:
            code, resp = await asyncio.to_thread(            # blocking Fatoora gateway OFF the event loop
                call, signed_xml=inv.signed_xml, invoice_hash=inv.hash,
                uuid=inv.uuid, credentials=creds)
        except FatooraUnavailable as ex:
            return await self._defer(inv, str(ex))
        outcome = classify(code, resp, success_status=success)
        inv.warnings, inv.errors = outcome["warnings"], outcome["errors"]
        inv.message = outcome["message"] or ""
        if outcome["outcome"] == success:
            inv.status = success
            inv.cleared_xml = outcome["cleared_xml"] or ""
            inv.next_retry_at = None
        elif outcome["outcome"] == "rejected":
            inv.status = "rejected"                         # terminal — fix & resubmit
            inv.next_retry_at = None
        else:
            await self._schedule_retry(inv)                 # transient → retry sweep
        await self._inv.save(inv)
        return self._result(inv)

    async def _defer(self, inv: ZatcaInvoice, message: str) -> dict[str, Any]:
        inv.message = message
        await self._schedule_retry(inv)
        await self._inv.save(inv)
        return self._result(inv)

    async def _schedule_retry(self, inv: ZatcaInvoice) -> None:
        sched = compute_retry_schedule(inv.attempts, base_ms=self._retry["base_ms"],
                                       max_ms=self._retry["max_ms"], max_attempts=self._retry["max_attempts"])
        inv.attempts += 1
        if sched["capped"]:
            # Retries exhausted: move OUT of the swept statuses to a terminal 'failed' (zatca-03), so
            # the retry sweep never reads it again. It was left 'pending' with next_retry_at=None, which
            # kept it in the swept set forever — making every sweep O(all unfinished invoices) instead
            # of O(due). Manual resubmit (which resets attempts) can still recover a 'failed' invoice.
            inv.next_retry_at = None
            inv.status = "failed"
        else:
            inv.next_retry_at = time.time() + sched["next_delay_ms"] / 1000.0

    # ── retry sweep / resubmit ──────────────────────────────────────────────────
    async def sweep(self) -> dict[str, Any]:
        due = await self._inv.due_for_retry(time.time(), self._limit)
        processed = 0
        for inv in due:
            secrets = await self._set.load_secrets(inv.tenant)
            if secrets is None:
                continue
            if inv.status in ("none", "pending"):
                if not await self._sign(inv, secrets):
                    continue
            await self._submit(inv, secrets)
            processed += 1
        return {"processed": processed, "due": len(due)}

    async def resubmit(self, invoice_id: str) -> dict[str, Any]:
        inv = await self._inv.get(invoice_id)
        if inv is None:
            return {"ok": False, "reason": "NOT_FOUND"}
        if inv.status in ACCEPTED:
            return {"ok": True, "reason": "already accepted", **self._result(inv)}
        secrets = await self._set.load_secrets(inv.tenant)
        if secrets is None:
            return {"ok": False, "reason": "NOT_ONBOARDED"}
        inv.attempts = 0                                    # manual resubmit restarts the clock
        # Re-sign whenever there is no signed body yet — including a 'failed' row that was capped
        # WHILE still awaiting signing (zatca-r2-06). The old guard (status in none/pending) skipped a
        # capped-unsigned invoice and then submitted an empty signed_xml the gateway hard-rejects,
        # leaving it permanently unrecoverable. A row that already has signed bytes is re-submitted
        # as-is (never re-signed — its bytes are sacrosanct).
        if not inv.signed_xml:
            if not await self._sign(inv, secrets):
                return {"ok": True, **self._result(inv)}
        res = await self._submit(inv, secrets)
        return {"ok": True, **res}

    # ── chain integrity ──────────────────────────────────────────────────────────
    async def verify_chain(self, tenant: str) -> dict[str, Any]:
        return _chain.verify_chain(await self._inv.chain_links(tenant))

    # ── onboarding ────────────────────────────────────────────────────────────────
    async def onboard_compliance(self, tenant: str, *, otp: str, csr_meta: dict) -> dict[str, Any]:
        csr = self._signer.generate_csr(csr_meta)          # raises SignerUnavailable if no signer
        code, resp = self._fatoora.onboard_compliance(csr_pem=csr["csrPem"], otp=otp)
        ok = 200 <= code < 300
        if ok:
            await self._set.store_csid(
                tenant, certificate_pem=resp.get("binarySecurityToken", ""),
                secret=resp.get("secret", ""), private_key_pem=csr.get("privateKeyPem", ""),
                onboarding_status="compliance", request_id=resp.get("requestID", ""))
        return {"code": code, "onboarding_status": "compliance" if ok else "failed",
                "request_id": resp.get("requestID")}

    async def onboard_production(self, tenant: str) -> dict[str, Any]:
        """Exchange the compliance CSID for a PRODUCTION CSID (the cert used to submit).
        Requires compliance onboarding first (CSID + request_id present)."""
        secrets = await self._set.load_secrets(tenant)
        req_id = await self._set.request_id(tenant)
        if secrets is None or not req_id:
            return {"code": 0, "onboarding_status": "failed", "reason": "NOT_COMPLIANCE_ONBOARDED"}
        code, resp = self._fatoora.onboard_production(
            compliance_request_id=req_id, csid_token=secrets["cert_base64"], csid_secret=secrets["secret"])
        ok = 200 <= code < 300
        if ok:                                             # keep the compliance private key (unchanged)
            await self._set.store_csid(
                tenant, certificate_pem=resp.get("binarySecurityToken", ""),
                secret=resp.get("secret", ""), onboarding_status="production")
        return {"code": code, "onboarding_status": "production" if ok else "failed"}

    def _result(self, inv: ZatcaInvoice) -> dict[str, Any]:
        return {"invoice_id": inv.id, "number": inv.number, "icv": inv.icv, "uuid": inv.uuid,
                "status": inv.status, "invoice_type": inv.invoice_type,
                # The ZATCA TLV QR, base64. RETURNED, because it is generated here and printed
                # somewhere else: it was stored on the invoice and never handed back, so no PDF
                # could show it — and a KSA tax invoice without the QR is not a compliant invoice.
                # It carries seller name, VAT number, timestamp and totals, all of which are meant
                # to be readable by anyone holding the printed invoice, so returning it discloses
                # nothing that printing it would not.
                "qr": inv.qr,
                "warnings": inv.warnings, "errors": inv.errors, "message": inv.message}
