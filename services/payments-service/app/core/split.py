"""Split-payment planning — SERVER-AUTHORITATIVE and FAIL-CLOSED.

The one rule that keeps split money correct: the SERVER decides the split, never the browser/caller's
word for it, and a split that does not reconcile against the recorded total is REFUSED — nothing is
minted. The platform's commission is the IMPLICIT remainder (`total - sum(shares)`), so there is no
attacker-settable "fee" field: a caller can only say how much each sub-merchant gets, and whatever is
left is the platform's — never negative.

Invariants enforced here (any violation raises SplitError and mutates nothing):
  * total > 0; every share is a positive int; every destination id is non-empty;
  * no DUPLICATE destination id (a duplicate could double-route to one sub-merchant, or mask a second
    hidden share);
  * sum(shares) <= total  ⇒  platform_fee = total - sum(shares) >= 0  (never overspend the charge);
  * one currency for the whole split (money in two currencies is never summed).
"""
from __future__ import annotations

from dataclasses import dataclass

from app.ports.payment_gateway import SplitDestination


class SplitError(ValueError):
    """A split that cannot be applied safely — refused, nothing minted."""


@dataclass(frozen=True)
class SplitPlan:
    total_minor: int
    currency: str
    destinations: tuple[SplitDestination, ...]
    platform_fee_minor: int          # the implicit remainder that stays with the platform account

    def to_dict(self) -> dict:
        return {"total_minor": self.total_minor, "currency": self.currency,
                "platform_fee_minor": self.platform_fee_minor,
                "destinations": [d.to_dict() for d in self.destinations]}


def _coerce_destination(raw) -> SplitDestination:
    """Accept a SplitDestination or a {destination_id, amount_minor} mapping; reject anything else."""
    if isinstance(raw, SplitDestination):
        dest_id, amount = raw.destination_id, raw.amount_minor
    elif isinstance(raw, dict):
        dest_id = raw.get("destination_id") or raw.get("id") or ""
        amount = raw.get("amount_minor", raw.get("amount"))
    else:
        raise SplitError("each destination must be a mapping with destination_id + amount_minor")
    if not isinstance(dest_id, str) or not dest_id.strip():
        raise SplitError("each destination needs a non-empty destination_id")
    # Reject a bool (bool is an int subclass — True would sneak through as 1) and any non-int amount.
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise SplitError(f"destination {dest_id!r} amount_minor must be an integer in minor units")
    if amount <= 0:
        raise SplitError(f"destination {dest_id!r} amount_minor must be positive")
    return SplitDestination(destination_id=dest_id.strip(), amount_minor=amount)


def plan_split(*, total_minor: int, currency: str, destinations) -> SplitPlan:
    """Validate + compute the split. Raises SplitError (fail-closed) on any violation; returns a
    SplitPlan whose platform_fee is the non-negative remainder."""
    if isinstance(total_minor, bool) or not isinstance(total_minor, int) or total_minor <= 0:
        raise SplitError("total_minor must be a positive integer in minor units")
    if not currency or not isinstance(currency, str):
        raise SplitError("a split needs a currency")
    if not destinations:
        raise SplitError("a split needs at least one destination")

    coerced = [_coerce_destination(d) for d in destinations]
    seen: set[str] = set()
    for d in coerced:
        if d.destination_id in seen:
            raise SplitError(f"duplicate destination_id {d.destination_id!r} in the split")
        seen.add(d.destination_id)

    shares = sum(d.amount_minor for d in coerced)
    if shares > total_minor:
        # The sub-merchant shares exceed the charge — refuse rather than overspend it.
        raise SplitError(
            f"split shares ({shares}) exceed the total ({total_minor}); the remainder would be negative")
    platform_fee = total_minor - shares
    return SplitPlan(total_minor=total_minor, currency=currency.upper(),
                     destinations=tuple(coerced), platform_fee_minor=platform_fee)
