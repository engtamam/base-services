"""Domain models — Plan/Price, Coupon, Subscription, Invoice/LineItem, Usage, Payment.

Money is integer minor units (halalas/cents) end-to-end.
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


def now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Plan:
    id: str
    name: str
    # Each price is a plain dict, not a type — it is snapshotted onto the subscription verbatim,
    # so a dataclass here would be a second copy of a shape the store already round-trips:
    #   interval        month | year | once
    #   amount_minor    int
    #   currency        e.g. "SAR"
    #   type            flat (fixed per period) | per_seat (× quantity) | usage (metered)
    #                   | hybrid (base + overage)
    #   usage_metric    for usage/hybrid
    #   included_units  hybrid: free units before overage
    #   overage_minor   hybrid/usage: price per unit over included
    #   includes_tax    True → amount is VAT-inclusive (KSA display prices)
    prices: list[dict] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    trial_days: int = 0
    # Two separate switches, because "stop selling this" and "hide this" are different decisions.
    # active=False   nobody new may subscribe. Existing subscribers are UNAFFECTED — they bought
    #                terms, and retiring a plan is not a reason to stop honouring them.
    # listed=False   hidden from the public plan list but still subscribable, which is how a
    #                legacy or negotiated plan keeps working without being on the pricing page.
    active: bool = True
    listed: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Plan":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Coupon:
    code: str
    type: str = "percent"                # percent | fixed | trial
    value: int = 0                       # percent (0-100) | fixed minor | trial days
    currency: str = "SAR"
    # A window, not just an end. A campaign that starts on Sunday had to be created on Sunday,
    # by hand, because there was no way to say "valid from".
    starts_at: float | None = None
    expires_at: float | None = None
    max_redemptions: int = 0             # 0 = unlimited
    redeemed: int = 0                    # ENFORCED — see core/coupons.py
    # Scope the code to ONE plan (e.g. "15% off the الأساس plan"). Empty = applies to any plan, the
    # original behaviour. Enforced in subscribe(): a code that names a plan is REFUSED for a
    # subscription to a different plan (loud), never silently applied to nothing.
    plan_id: str = ""
    active: bool = True
    # A campaign the CUSTOMER app may show. Default False: a targeted or partner code is private,
    # and a public read must never leak one just because it is active. Opt IN to publish.
    public: bool = False
    created: str = ""
    note: str = ""                       # why this coupon exists; shown to operators, never to a buyer

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Coupon":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Subscription:
    id: str
    tenant: str
    plan_id: str
    interval: str = "month"
    status: str = "trialing"             # trialing|active|past_due|paused|canceled
    quantity: int = 1
    coupon_code: str = ""
    payment_token: str = ""              # provider token for the saved payment method
    buyer_name: str = ""                 # buyer identity for the tax invoice
    buyer_vat: str = ""                  # buyer VAT number (B2B → standard/clearance, reverse-charge)
    buyer_cr: str = ""                   # commercial registration
    # Grandfathering: terms captured at signup. Editing the catalogue never silently
    # re-prices or re-limits an existing customer — only an explicit migrate() does.
    price_snapshot: dict[str, Any] = field(default_factory=dict)
    limits_snapshot: dict[str, Any] = field(default_factory=dict)
    terms_version: str = ""              # when the snapshot was taken
    current_period_start: float = 0.0
    current_period_end: float = 0.0
    trial_end: float = 0.0
    # True only for a FIRST period that was shortened to align this subscription onto the tenant's
    # existing billing grid (a second+ subscription). The first run_cycle prorates the recurring fee
    # by the partial period and clears this; every cycle after is a full interval. Explicit rather than
    # inferred from period length, because a TRIAL also has a short first period and must NOT prorate.
    first_period_prorated: bool = False
    # A SCHEDULED DOWNGRADE, taking effect at the NEXT renewal — the Stripe-style shape that never
    # produces a credit. On a downgrade, change_plan() does NOT touch the live plan (the shop keeps the
    # more expensive plan it already paid for, in full, until the period ends) and records the target
    # here instead; the next run_cycle adopts it and invoices the new plan at FULL price for the new
    # period, no proration. Mirrors cancel_at_period_end (a scheduled state on this same model), and
    # cancel wins over it. An UPGRADE clears it (the shop chose to go up instead). Empty = none.
    pending_plan_id: str = ""
    pending_interval: str = ""
    pending_quantity: int = 0
    cancel_at_period_end: bool = False
    created: str = ""
    # When the subscription actually became `canceled` (ISO-8601), stamped at the transition — not to
    # be confused with cancel_at_period_end (a SCHEDULED intent that has not taken effect yet). Empty
    # until then. A platform view answers "how many canceled this month" off this, never off `created`.
    canceled_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Subscription":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Invoice:
    id: str
    tenant: str
    number: str = ""
    subscription_id: str = ""
    lines: list[dict] = field(default_factory=list)
    subtotal_minor: int = 0
    discount_minor: int = 0
    tax_minor: int = 0
    total_minor: int = 0
    currency: str = "SAR"
    status: str = "draft"                # draft|open|partially_paid|paid|uncollectible|void
    # Sum of CONFIRMED settlements. An invoice can be paid in tranches (a 60-day enterprise
    # often pays two), so `paid` is a transition, not the only way to have received money.
    settled_minor: int = 0
    # Money given BACK on this invoice — card refunds, credit notes, recalled settlements — accumulated
    # as they happen. Partial refunds were previously discoverable only in the ledger, so a partner
    # commission computed on the invoice charged on money that had returned to the customer. What the
    # shop actually KEPT paying is total_minor - refunded_minor.
    refunded_minor: int = 0
    credit_note: bool = False
    einvoice: dict[str, Any] = field(default_factory=dict)
    # What money document this invoice IS, FROZEN the moment it is paid — "receipt" | "tax_invoice"
    # (empty on a legacy row, inferred from `einvoice`). The document follows the INVOICE, not the
    # current setting: a mode switched after issue, or switched back, never retroactively changes a
    # document already produced. The render/download routes read THIS, not the live document mode.
    doc_type: str = ""
    # The seller's legal identity captured at issue when this was a tax invoice (name / VAT / CR /
    # address). Frozen here so re-rendering a delivered tax invoice shows the registration number it
    # was ISSUED with — never a later, emptied one — which would be forgery in good faith.
    seller: dict[str, Any] = field(default_factory=dict)
    # What this invoice entitles the buyer to beyond the money itself — currently `grant_minor`,
    # the wallet credits the plan included. Recorded ON THE INVOICE at the moment it is raised, so
    # a plan repriced afterwards cannot change what an already-sold invoice grants, and so a
    # reconciliation sweep can rebuild a lost grant from the invoice alone.
    meta: dict[str, Any] = field(default_factory=dict)
    created: str = ""
    paid_at: str = ""
    # When this invoice was VOIDED (a full refund, or accumulated partial refunds reaching the
    # total). ISO string like paid_at, "" while never voided. Status alone records THAT it voided;
    # a dispute timeline also needs WHEN — the exact instant the money was fully returned.
    voided_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Invoice":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Settlement:
    """A payment that never touched a gateway: a wire, a cheque, cash, an offline terminal.

    Immutable once written. Its real-world identity is (method, issuer, reference) — a bank
    reference is unique per BANK, not per planet, so the issuer is part of the key.
    """
    id: str
    tenant: str
    invoice_id: str
    method: str = ""                     # from SETTLEMENT_METHODS; never hardcoded
    issuer: str = ""                     # the bank/clearing house that minted the reference
    reference: str = ""
    amount_minor: int = 0
    currency: str = "SAR"
    paid_at: str = ""                    # the BANK's value date, not when we recorded it
    proof_file_id: str = ""              # file-service id; billing never stores the blob
    actor: str = ""                      # a person read a bank statement; audit needs a name
    note: str = ""
    status: str = "confirmed"            # confirmed|reversed
    reversed_reason: str = ""
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Settlement":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Payment:
    id: str
    tenant: str
    invoice_id: str
    provider: str = ""
    provider_id: str = ""
    amount_minor: int = 0
    currency: str = "SAR"
    status: str = "pending"             # pending|succeeded|failed|refunded
    attempts: int = 0
    next_retry_at: float | None = None
    error: str = ""
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Payment":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
