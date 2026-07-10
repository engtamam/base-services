"""VAT calculator — config rate (KSA 15%), exemption + B2B reverse-charge => 0."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


class VatCalculator:
    driver = "vat"

    def __init__(self, rate: str = "0.15") -> None:
        self._rate = Decimal(str(rate))

    def compute(self, *, amount_minor: int, exempt: bool = False, reverse_charge: bool = False) -> int:
        if exempt or reverse_charge:
            return 0
        return int((Decimal(int(amount_minor)) * self._rate).quantize(Decimal(1), rounding=ROUND_HALF_UP))
