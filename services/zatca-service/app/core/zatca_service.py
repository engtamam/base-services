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


class ZatcaService:
    def __init__(self, *, invoices: InvoiceStore, settings: SettingsStore, signer, fatoora,
                 qr, vat_rate: str, currency: str, retry: dict[str, int], list_limit: int = 100,
                 validate_before_submit: bool = False) -> None:
        self._inv = invoices
        self._set = settings
        self._signer = signer
        self._fatoora = fatoora
        self._qr = qr
        self._vat_rate = vat_rate
        self._currency = currency
        self._retry = retry
        self._limit = list_limit
        self._validate = validate_before_submit

    # ── issuance ────────────────────────────────────────────────────────────────
    async def finalize(self, invoice_in: dict, tenant: str) -> dict[str, Any]:
        simplified = not (invoice_in.get("buyer") or {}).get("vat_number")
        itype = "simplified" if simplified else "standard"
        inv, _created = await self._inv.allocate_slot(tenant, str(invoice_in["number"]), itype)
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
        d = inv.draft
        try:
            signed = self._signer.sign(
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
        await self._inv.record_hash(inv)                   # save + advance chain head (O(1))
        return True

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
            code, resp = call(signed_xml=inv.signed_xml, invoice_hash=inv.hash,
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
        inv.next_retry_at = None if sched["capped"] else time.time() + sched["next_delay_ms"] / 1000.0

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
        if inv.status in ("none", "pending"):
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
