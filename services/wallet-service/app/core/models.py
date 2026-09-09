"""Domain models — Wallet, Hold, LedgerEntry, Grant. All amounts are integer minor units."""
from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

# Ledger entry types. `spend` and `refund` are mirror images; a refund names the spend it undoes.
CREDIT = "credit"
SPEND = "spend"
REFUND = "refund"
CLAWBACK = "clawback"
# Grant expiry, two DISTINCT debits — the difference is the whole point of the reclaimable model:
#   clawback — FORFEITURE. The money is gone; nothing here reverses it. For a fund whose credit may
#              legally lapse (a non-refundable service credit, a promo).
#   expire   — DORMANCY. The money leaves the SPENDABLE balance but is PRESERVED and RECLAIMABLE: the
#              grant goes state="dormant" and a later `reclaim` credits it back (RECLAIM). This is the
#              SAMA "unclaimed balances" shape for stored value that must not be silently forfeited.
EXPIRE = "expire"
RECLAIM = "reclaim"
# A payout leaving the platform (a debit), and its reversal when the transfer fails (a credit back).
WITHDRAW = "withdraw"
WITHDRAW_REVERSAL = "withdraw_reversal"
# Ledger types that ADD to the balance (everything else is a debit). reconcile() and any balance
# derivation must read this, or a reversal would be counted the wrong way. RECLAIM restores a dormant
# grant, so it credits.
CREDIT_TYPES = (CREDIT, REFUND, WITHDRAW_REVERSAL, RECLAIM)

WITHDRAW_PENDING = "pending"        # debited, awaiting the external transfer
WITHDRAW_PAID = "paid"              # the transfer settled
WITHDRAW_FAILED = "failed"          # the transfer failed; the debit was reversed

HOLD_ACTIVE = "active"
HOLD_CONFIRMED = "confirmed"
HOLD_RELEASED = "released"
HOLD_EXPIRED = "expired"


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex}"


@dataclass
class Wallet:
    """The MATERIALIZED balance. The ledger is the truth; this is a projection of it.

    Both move in one atomic step (compare-and-swap on `version`), and `reconcile()` proves
    they agree. Summing the ledger on every read would be an O(n) hot path that grows forever;
    trusting this row without a reconcile is how a wallet quietly drifts from its own history.
    """
    tenant: str
    funds: dict[str, int] = field(default_factory=dict)   # fund name -> minor units
    # Reserved totals live HERE, not derived by scanning the holds table — that would put a
    # full scan on every spend. `available = funds - held`.
    held: dict[str, int] = field(default_factory=dict)
    version: int = 0                                      # compare-and-swap guard
    seq: int = 0                                          # ledger sequence for this tenant
    # The ledger row implied by the last CAS, staged so a crash before the append can replay
    # it. Money never moves without its record.
    pending: dict[str, Any] | None = None
    updated: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Wallet":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class LedgerEntry:
    """Immutable. Never updated, never deleted — invoices and audits derive from these rows."""
    id: str
    tenant: str
    seq: int
    type: str                                             # credit|spend|refund|clawback
    # Per-fund movement. A refund MUST return money to the funds the spend took it from:
    # refunding an expiring gift into cash silently converts it into real money.
    applied: dict[str, int] = field(default_factory=dict)
    net: int = 0
    vat: int = 0
    discount: int = 0
    amount: int = 0                                       # signed total: negative = money out
    balance_after: int = 0
    currency: str = ""
    source: str = ""                                      # the idempotency key of the operation
    reason: str = ""
    ref: str = ""                                         # a refund names the spend it reverses
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LedgerEntry":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Hold:
    """Reserved-but-not-spent money.

    `expires_at` is not optional. A hold with no expiry, and no sweeper to enforce it, locks a
    customer's credits forever the moment their phone dies mid-request — while the balance
    still cheerfully displays them.
    """
    id: str
    tenant: str
    applied: dict[str, int] = field(default_factory=dict)  # the planned per-fund split
    plan: dict[str, Any] = field(default_factory=dict)     # net/vat/gross, recorded at reserve
    state: str = HOLD_ACTIVE
    # True once the reservation has actually landed in the wallet's `held` totals. A crash
    # between writing this row and the CAS leaves counted=False, and the sweeper discards it
    # rather than decrementing a reservation that was never counted.
    counted: bool = False
    expires_at: float = 0.0
    source: str = ""
    reason: str = ""
    created: float = 0.0

    @property
    def amount(self) -> int:
        return sum(int(v) for v in self.applied.values())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Hold":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Grant:
    """A credit that expires (a welcome gift). The customer keeps what they spent."""
    id: str
    tenant: str
    fund: str = ""
    amount: int = 0
    expires_at: float = 0.0
    state: str = "active"                                  # active|expired|dormant|reclaimed
    swept: int = 0                                         # amount removed from spendable at expiry
    noticed: int = 0                                       # 1 once a pre-expiry notice was emitted
    # The fund's expiry policy FROZEN at credit time — the whole point of the reclaimable model is that
    # a later config change (or a deleted policy) can NEVER retroactively turn a preserved (dormant)
    # grant into a forfeited one. A non-empty idle_behavior marks a grant as frozen; a legacy grant
    # (created before this field existed) has "" and falls back to the live policy at sweep time.
    idle_behavior: str = ""                               # frozen: ""|forfeit|dormant|extend
    ttl_days: int = 0                                     # frozen: the extension length for `extend`
    notice_days: int = 0                                  # frozen: pre-expiry notice window
    created: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Grant":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Withdrawal:
    """A payout: money leaving the platform to an external account. The debit lands the instant the
    request is accepted (the funds are earmarked out of the spendable balance); the external transfer
    is executed by a payout sidecar and its result is recorded by settle(). A failed transfer credits
    the debit back — the customer never loses money to a transfer that did not happen."""
    id: str
    tenant: str
    amount: int = 0
    applied: dict = None                                   # fund name -> minor units debited
    destination: str = ""                                  # opaque payout target (a masked ref)
    state: str = "pending"                                 # pending | paid | failed
    source: str = ""                                       # idempotency source of the request
    reason: str = ""
    created: float = 0.0
    settled_at: float = 0.0

    def __post_init__(self) -> None:
        if self.applied is None:
            self.applied = {}

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Withdrawal":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
