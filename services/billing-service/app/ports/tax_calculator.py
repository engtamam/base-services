"""TaxCalculator port — compute tax on an amount (VAT, exemptions, reverse-charge)."""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class TaxCalculator(Protocol):
    driver: str

    def compute(self, *, amount_minor: int, exempt: bool = False,
                reverse_charge: bool = False) -> int:
        """Return the tax in minor units (0 when exempt or B2B reverse-charge)."""
        ...
