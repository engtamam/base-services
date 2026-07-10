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


# Pricing types: flat (fixed per period) · per_seat (× quantity) · usage (metered) · hybrid (base + overage)
@dataclass
class Price:
    interval: str = "month"              # month | year | once
    amount_minor: int = 0
    currency: str = "SAR"
    type: str = "flat"                   # flat | per_seat | usage | hybrid
    usage_metric: str = ""               # for usage/hybrid
    included_units: int = 0              # hybrid: free units before overage
    overage_minor: int = 0               # hybrid/usage: price per unit over included
    includes_tax: bool = False           # True → amount is VAT-inclusive (KSA display prices)


@dataclass
class Plan:
    id: str
    name: str
    prices: list[dict] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    limits: dict[str, Any] = field(default_factory=dict)
    trial_days: int = 0
    active: bool = True

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
    expires_at: float | None = None
    max_redemptions: int = 0             # 0 = unlimited
    redeemed: int = 0
    active: bool = True

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
    cancel_at_period_end: bool = False
    created: str = ""

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
    credit_note: bool = False
    einvoice: dict[str, Any] = field(default_factory=dict)
    created: str = ""
    paid_at: str = ""

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
