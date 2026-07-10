"""Funds — how a spend is split across a tenant's balances. Pure, and fund-name-agnostic.

The funds come from `WALLET_FUNDS`. This module never says "promo" or "cash"; it walks the
configured funds in priority order. A one-fund wallet works. A five-fund wallet works.

The rule that matters, and the reason funds have a `taxable` flag at all:

    An UNTAXED fund is a PRE-VAT DISCOUNT. A gift carries no VAT.
    VAT is charged on the taxable remainder. The TAXED funds then cover that gross.

Charging VAT on a promotional credit overcharges the customer and files the wrong tax. Applying
promo *after* VAT does the same thing more subtly. So the order is: discount, then tax, then pay.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.money import gross_of, require_non_negative


class InsufficientFunds(Exception):
    """Never partially applied: the caller's balances are untouched."""

    def __init__(self, needed: int, available: int) -> None:
        super().__init__(f"INSUFFICIENT_FUNDS: need {needed}, have {available}")
        self.needed, self.available = needed, available


@dataclass(frozen=True)
class Fund:
    name: str
    priority: int
    taxable: bool


def parse(spec: tuple[tuple[str, int, bool], ...]) -> tuple[Fund, ...]:
    return tuple(Fund(n, p, t) for n, p, t in spec)


@dataclass(frozen=True)
class SpendPlan:
    """Exactly how a spend lands. `applied` is what each fund pays, in minor units."""
    net: int                        # the pre-VAT price
    discount: int                   # covered by untaxed funds (carries no VAT)
    taxable_net: int                # net - discount
    vat: int
    gross: int                      # taxable_net + vat  (what the taxed funds must cover)
    applied: dict[str, int]         # fund name -> minor units taken

    def to_dict(self) -> dict[str, Any]:
        return {"net": self.net, "discount": self.discount, "taxable_net": self.taxable_net,
                "vat": self.vat, "gross": self.gross, "applied": dict(self.applied)}


def plan_spend(net_minor: int, balances: dict[str, int], funds: tuple[Fund, ...],
               vat_bp: int) -> SpendPlan:
    """Decide the split WITHOUT mutating anything. Raises rather than overdraw.

    Untaxed funds discount the net (in priority order). VAT applies to what is left. Taxed
    funds then cover the gross. A fund is never driven below zero.
    """
    net = require_non_negative(net_minor, "net")
    applied: dict[str, int] = {}

    # 1. Untaxed funds discount the NET, before any VAT is computed.
    remaining_net = net
    for f in funds:
        if f.taxable or remaining_net <= 0:
            continue
        take = min(int(balances.get(f.name, 0)), remaining_net)
        if take > 0:
            applied[f.name] = take
            remaining_net -= take

    # 2. VAT on the taxable remainder.
    taxable_net, vat, gross = gross_of(remaining_net, vat_bp)

    # 3. Taxed funds cover the gross.
    remaining_gross = gross
    for f in funds:
        if not f.taxable or remaining_gross <= 0:
            continue
        take = min(int(balances.get(f.name, 0)), remaining_gross)
        if take > 0:
            applied[f.name] = applied.get(f.name, 0) + take
            remaining_gross -= take

    if remaining_gross > 0:
        # Nothing has been mutated: the plan simply cannot be paid. Fail closed — a wallet
        # that goes negative is a wallet that gave away the product.
        payable = sum(int(v) for v in balances.values())
        raise InsufficientFunds(needed=gross, available=payable)

    return SpendPlan(net=net, discount=net - taxable_net, taxable_net=taxable_net,
                     vat=vat, gross=gross, applied=applied)


def apply(balances: dict[str, int], applied: dict[str, int], sign: int = -1) -> dict[str, int]:
    """Return NEW balances with `applied` debited (sign=-1) or credited back (sign=+1)."""
    out = dict(balances)
    for name, amount in applied.items():
        out[name] = int(out.get(name, 0)) + sign * int(amount)
        if out[name] < 0:
            raise InsufficientFunds(needed=int(amount), available=int(balances.get(name, 0)))
    return out


def total(balances: dict[str, int]) -> int:
    return sum(int(v) for v in balances.values())


def available(balances: dict[str, int], held: dict[str, int]) -> dict[str, int]:
    """Balance minus what is already reserved.

    Spending against `balances` instead of `available` makes a hold decoration: two requests
    both see the money and both spend it.
    """
    return {k: int(v) - int(held.get(k, 0)) for k, v in balances.items()}
