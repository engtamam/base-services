"""Off-gateway settlement — pure rules. No method is ever named in this file.

A payment that never touched a gateway: a wire, a cheque, cash, an offline card terminal.
Saudi enterprises buy on a purchase order and pay by bank transfer 30-60 days later; above
~SAR 100k nobody pays by card. But nothing here is Saudi, and nothing here is a bank: the
methods come from `SETTLEMENT_METHODS`, and an unknown one fails closed.

THE IDENTITY OF A PAYMENT is the triple:

    (method, issuer, reference)

A bank reference is unique **per bank, not per planet**. Two banks can mint the same string,
and both payments are real. `(method, reference)` collides for the same reason — two banks,
both `bank_transfer`. So the issuer, the institution that minted the reference, is part of the
key; it is what makes the reference mean anything.

`issuer` cannot be blindly required: cash has no issuer. Whether a method carries one is
declared WITH the method, in config, never as a branch in code. For an issuer-less method the
tenant stands in, keeping the triple total: cash receipt `0001` in two tenants is two payments.

Scoping the key does not make it safe to talk about. Whatever the key, an error naming the
owner of an existing settlement turns this endpoint into an oracle: probe a triple, learn
whether another tenant received that transfer. The duplicate error is GENERIC.
"""
from __future__ import annotations

import datetime as _dt
from typing import Any

CONFIRMED = "confirmed"
REVERSED = "reversed"

# Invoice statuses a settlement may be applied to. A `draft` has no number yet; a `void` is
# gone; a `paid` one is done. Settling any of them is a bug in the caller, not a rounding case.
SETTLEABLE = ("open", "partially_paid")


class SettlementError(ValueError):
    """A settlement that must not be recorded. Fails closed; nothing is written."""


def identity(tenant: str, method: str, issuer: str, reference: str) -> str:
    """The key of ONE real-world payment.

    Note the tenant is NOT in the key for issuer-bearing methods: a single wire must not be
    able to settle an invoice in two tenants. For issuer-less methods the tenant IS the issuer,
    so the key is naturally tenant-scoped.
    """
    return f"{method}|{issuer}|{reference}".lower()


def normalize(*, tenant: str, method: str, issuer: str, reference: str,
              methods: dict[str, bool]) -> tuple[str, str, str]:
    """Validate the triple against the configured methods. Returns (method, issuer, reference)."""
    method = (method or "").strip()
    if method not in methods:
        # Never echo the allowlist: it is operator configuration, not caller business.
        raise SettlementError("UNKNOWN_SETTLEMENT_METHOD")

    reference = (reference or "").strip()
    if not reference:
        raise SettlementError("REFERENCE_REQUIRED")

    issuer = (issuer or "").strip()
    if methods[method]:
        if not issuer:
            raise SettlementError("ISSUER_REQUIRED_FOR_METHOD")
    else:
        if issuer:
            raise SettlementError("ISSUER_NOT_ALLOWED_FOR_METHOD")
        issuer = tenant          # keeps the triple total; scopes issuer-less methods per tenant
    return method, issuer, reference


def check_paid_at(paid_at: float, *, now: float, invoice_created: float,
                  max_backdate_days: int) -> float:
    """The bank's VALUE DATE, not `now()`.

    Stamping `now()` files the tax invoice in the wrong period and recognises revenue in the
    wrong month. The date is in the past by construction; unbounded, it rewrites a closed
    period, so it is bounded — and it can never precede the invoice it settles.
    """
    ts = float(paid_at)
    if ts > now + 60:                                   # a minute of clock skew, no more
        raise SettlementError("PAID_AT_IN_THE_FUTURE")
    if invoice_created and ts < invoice_created - 86_400:
        raise SettlementError("PAID_AT_BEFORE_INVOICE")
    if max_backdate_days > 0 and ts < now - max_backdate_days * 86_400:
        raise SettlementError("PAID_AT_TOO_OLD")
    return ts


def check_amount(*, amount_minor: int, currency: str, invoice_currency: str,
                 invoice_total_minor: int, already_settled_minor: int,
                 allow_partial: bool, tolerance_minor: int) -> dict[str, Any]:
    """Decide how much this settlement actually settles, and whether it closes the invoice.

    Returns {applied, remaining_after, closes}. `closes` is the ONLY thing that may finalize
    an e-invoice: never per tranche — ZATCA gets one invoice, once.
    """
    if int(amount_minor) <= 0:
        raise SettlementError("AMOUNT_MUST_BE_POSITIVE")
    # A USD wire cannot settle a SAR invoice. Refuse; never convert silently.
    if (currency or "").upper() != (invoice_currency or "").upper():
        raise SettlementError("CURRENCY_MISMATCH")

    amount = int(amount_minor)
    outstanding = int(invoice_total_minor) - int(already_settled_minor)
    if outstanding <= 0:
        raise SettlementError("INVOICE_ALREADY_SETTLED")
    if amount > outstanding:
        # Overpayment is a human error with tax consequences. Refuse it rather than invent a
        # credit the operator never asked for.
        raise SettlementError("AMOUNT_EXCEEDS_OUTSTANDING")

    remaining = outstanding - amount
    # The correspondent bank's fee: within tolerance the invoice is considered settled.
    closes = remaining <= max(0, int(tolerance_minor))
    if not closes and not allow_partial:
        raise SettlementError("PARTIAL_SETTLEMENT_NOT_ALLOWED")
    return {"applied": amount, "remaining_after": remaining, "closes": closes}


def check_status(status: str) -> None:
    if status not in SETTLEABLE:
        raise SettlementError(f"INVOICE_NOT_SETTLEABLE:{status}")


def iso_of(ts: float) -> str:
    return _dt.datetime.fromtimestamp(float(ts), _dt.timezone.utc).isoformat()
