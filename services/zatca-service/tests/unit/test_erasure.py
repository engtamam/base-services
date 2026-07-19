"""Erasure — zatca. The tests exist to prove what MUST NOT happen.

Every other service's erasure tests ask "was it deleted?". Here the load-bearing question is the
opposite: a signed invoice must come out of a DSAR byte-for-byte identical, because altering it
invalidates the signature and every invoice chained after it.
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.erasure import SIGNATURE_BASIS, TAX_BASIS, ZatcaErasure

_SALT = "s" * 32
_SUBJECT = "buyer@example.com"


def run(c):
    return asyncio.run(c)


class _Store:
    def __init__(self, rows):
        self.rows = rows

    async def find(self, collection, filt, limit=100, after=None, sort=None):
        start = int(after or 0)
        page = self.rows[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(self.rows) else None)

    async def put(self, collection, key, value):
        for i, r in enumerate(self.rows):
            if r["id"] == key:
                self.rows[i] = value


def _inv(id, status="cleared", buyer=_SUBJECT, signed="<Invoice>SIGNED</Invoice>"):
    return {"id": id, "tenant": "acme", "status": status, "created": "2026-01-01",
            "signed_xml": signed, "cleared_xml": "<Cleared/>", "hash": "h_" + id,
            "draft": {"unsigned_xml": f"<Invoice><Buyer>{buyer}</Buyer></Invoice>",
                      "seller_name": "Acme Ltd", "invoice_total": "115.00"}}


def _mk(store):
    return ZatcaErasure(store=store, pii_salt=_SALT)


def test_the_signed_invoice_is_never_touched():
    # THE test. A changed byte here invalidates the signature and every later invoice's PIH chain.
    store = _Store([_inv("i1")])
    before = dict(store.rows[0])
    run(_mk(store).erase("acme", _SUBJECT))
    after = store.rows[0]
    assert after["signed_xml"] == before["signed_xml"]
    assert after["cleared_xml"] == before["cleared_xml"]
    assert after["hash"] == before["hash"]


def test_the_draft_working_copy_of_a_finalised_invoice_is_erased():
    store = _Store([_inv("i1")])
    out = run(_mk(store).erase("acme", _SUBJECT))
    assert "unsigned_xml" not in store.rows[0]["draft"]
    assert out["erased"] == ["invoice_draft_xml:1"]
    # the non-PII parts of the draft survive — we removed the person, not the record
    assert store.rows[0]["draft"]["invoice_total"] == "115.00"


@pytest.mark.parametrize("status", ["none", "pending", "signed"])
def test_a_live_invoice_keeps_its_draft_so_it_can_still_be_signed(status):
    # Dropping the draft before the invoice is final would strand an invoice that cannot be
    # re-signed after a signer outage — an erasure that breaks tax compliance.
    store = _Store([_inv("i1", status=status)])
    out = run(_mk(store).erase("acme", _SUBJECT))
    assert "unsigned_xml" in store.rows[0]["draft"]
    assert out["erased"] == []


def test_the_ack_explains_the_refusal_with_a_legal_basis():
    # "We refuse" without a reason reads as non-compliance. The person must get the law.
    out = run(_mk(_Store([_inv("i1")])).erase("acme", _SUBJECT))
    bases = {r["basis"] for r in out["retained"]}
    assert bases == {TAX_BASIS, SIGNATURE_BASIS}
    why = " ".join(r["why"] for r in out["retained"])
    assert "6 years" in why                      # the retention period is quoted back
    assert "PIH" in why and "signature" in why   # and WHY pseudonymisation is impossible here


def test_another_persons_invoice_is_left_alone():
    store = _Store([_inv("mine"), _inv("theirs", buyer="someone.else@example.com")])
    run(_mk(store).erase("acme", _SUBJECT))
    theirs = [r for r in store.rows if r["id"] == "theirs"][0]
    assert "unsigned_xml" in theirs["draft"]


def test_a_subject_with_no_invoices_acks_already_absent():
    out = run(_mk(_Store([_inv("i1", buyer="other@example.com")])).erase("acme", _SUBJECT))
    assert out["already_absent"] is True and out["retained"] == [] and out["erased"] == []


def test_erasing_twice_is_a_no_op_that_still_acks():
    # A DSAR retries; the second call must not fail.
    store = _Store([_inv("i1")])
    first = run(_mk(store).erase("acme", _SUBJECT))
    second = run(_mk(store).erase("acme", _SUBJECT))
    assert first["erased"] == ["invoice_draft_xml:1"]
    assert second["erased"] == [] and second["already_absent"] is True


def test_the_ack_carries_a_pseudonym_not_the_person():
    out = run(_mk(_Store([_inv("i1")])).erase("acme", _SUBJECT))
    assert out["subject_ref"].startswith("sub_")
    assert _SUBJECT not in out["subject_ref"]


def test_the_scan_is_bounded():
    store = _Store([_inv(f"i{i}") for i in range(50)])
    out = run(ZatcaErasure(store=store, pii_salt=_SALT, max_scan=10).erase("acme", _SUBJECT))
    assert out["truncated"] is True
