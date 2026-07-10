"""Null tax — always zero (non-taxable / dev)."""
from __future__ import annotations


class NullTax:
    driver = "null"

    def compute(self, *, amount_minor: int, exempt: bool = False, reverse_charge: bool = False) -> int:
        return 0
