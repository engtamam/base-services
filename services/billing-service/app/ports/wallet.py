"""Wallet port — where a paid-for credit is actually delivered.

WHY THIS IS NOT AN EVENT. The event bus is at-most-once and best effort: a subscriber that is
restarting when `payment.succeeded` fires never receives it, and nothing anywhere records that it
missed one. Crediting a wallet that way means a customer pays and their credits never arrive — not
late, never, because nothing will ever say it again.

So the money consequence goes through a port with a durable outbox in front of it, and the event
stays what the bus can honestly support: a hint that lets anyone else react.

IDEMPOTENCY IS THE CALLER'S JOB AND IT IS NOT OPTIONAL. `source` is a deterministic key derived
from the thing that was paid for, never a timestamp or a random id — the outbox retries, and a
reconciliation sweep re-reads the same invoices, so the same credit WILL be offered more than once.
The receiving service de-duplicates on it. A credit granted twice is money given away; a credit
granted never is money taken.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


class WalletUnavailable(RuntimeError):
    """The wallet could not be reached, or refused in a way worth retrying.

    Deliberately distinct from a permanent refusal: a 503 should be retried until it works, while
    an UNKNOWN_FUND never will be, and retrying it for ever hides a misconfiguration behind a queue
    that only ever grows.
    """


class WalletRefused(ValueError):
    """The wallet rejected this credit permanently. Retrying cannot help."""


@runtime_checkable
class Wallet(Protocol):
    driver: str

    async def credit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                     reason: str = "", expires_at: float | None = None) -> bool:
        """Add funds, idempotently on `source`. True when the credit is durably applied.

        `expires_at` is how a plan whose credits do NOT roll over is expressed: the grant simply
        dies with the period. Expressing it as an expiry rather than a "zero the balance first"
        step matters — zeroing would also destroy credits the customer bought separately, which
        they paid for and which have nothing to do with the plan's monthly allowance.

        Raises WalletUnavailable for anything worth retrying and WalletRefused for anything that
        never will be.
        """
        ...

    async def debit(self, *, tenant: str, fund: str, amount_minor: int, source: str,
                    reason: str = "", allow_negative: bool = False) -> bool:
        """Take funds back, idempotently on `source`. True when the debit is durably applied.

        This is how a refund or chargeback reverses the credits the payment bought. `source` is a
        DISTINCT clawback key (never the original credit's), so a redelivered clawback debits once
        and is never confused with the credit it undoes.

        `allow_negative` is the crux of a clawback: the customer may already have SPENT the credits
        being refunded, so refusing to go below zero would let them keep both the money and the
        goods. With it True the balance is allowed below zero; with it False an insufficient balance
        is a permanent `WalletRefused` the caller turns into a `grant.clawback_failed` signal rather
        than a retry-for-ever.

        Raises WalletUnavailable for anything worth retrying and WalletRefused for anything that
        never will be (including insufficient balance when `allow_negative` is False).
        """
        ...

    async def health(self) -> bool: ...
