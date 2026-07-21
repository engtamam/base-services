"""Turning stored invoice values into what a human may read on a tax document.

THIS EXISTS BECAUSE THE PDF WAS PRINTING MINOR UNITS RAW. An invoice for 115.00 SAR displayed
`11500`. That is not a cosmetic defect — it is a tax document stating the wrong amount, and it
would have been read by a customer, an auditor, and ZATCA.

Money is stored in minor units because integers do not drift the way floats do. That is correct.
It also means every value must be scaled by the currency's own exponent before a person sees it,
and the exponent is NOT always two: KWD, BHD, OMR and JOD have three, and JPY has none. Assuming
two would overstate a Kuwaiti invoice by a factor of ten and understate a Japanese one by a
hundred.

FORMATTING IS DONE WITH INTEGERS AND STRING SLICING, never floats. `11500 / 100` is fine for these
magnitudes and wrong for larger ones, and "fine until the number gets big" is not a property worth
having in an invoice.
"""
from __future__ import annotations

# ISO 4217 exponents that are not 2. Listed explicitly rather than pulled from a library, because
# a currency table is small, auditable, and something an accountant can check by reading it.
_EXPONENT = {
    # No minor unit at all.
    "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BIF": 0, "DJF": 0, "GNF": 0, "KMF": 0, "MGA": 0, "PYG": 0, "RWF": 0, "UGX": 0, "VUV": 0,
    # Three decimal places — four of these are Gulf currencies, so they are not an edge case for
    # this platform, they are the neighbours.
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}
_DEFAULT_EXPONENT = 2


def exponent(currency: str) -> int:
    """How many decimal places this currency has."""
    return _EXPONENT.get((currency or "").strip().upper(), _DEFAULT_EXPONENT)


def money(amount_minor: int | float | str | None, currency: str = "", *,
          with_currency: bool = True, group: bool = True) -> str:
    """A minor-unit amount as a person reads it: 11500 SAR -> "115.00 SAR".

    Negative amounts keep their sign in front of the digits rather than being wrapped in
    parentheses: a credit note that reads (115.00) has been mistaken for a positive amount often
    enough that the sign is worth the plainness.
    """
    # An int is used AS an int. Routing it through float() first — which the first version of
    # this did, while the module docstring claimed otherwise — silently rounds any amount past
    # 2^53 to the nearest representable double. Invoices that large are rare and "rare" is not the
    # same as "never", and a rounding error in a tax total is not something to discover from a
    # customer.
    if isinstance(amount_minor, int) and not isinstance(amount_minor, bool):
        minor = amount_minor
    else:
        try:
            minor = int(round(float(amount_minor or 0)))
        except (TypeError, ValueError):
            minor = 0
    exp = exponent(currency)
    sign = "-" if minor < 0 else ""
    digits = str(abs(minor)).rjust(exp + 1, "0")
    whole, frac = (digits[:-exp], digits[-exp:]) if exp else (digits, "")
    if group:
        whole = f"{int(whole):,}"
    out = f"{sign}{whole}.{frac}" if exp else f"{sign}{whole}"
    cur = (currency or "").strip().upper()
    return f"{out} {cur}" if with_currency and cur else out


def percent(rate: str | float | None) -> str:
    """A VAT rate as a percentage. Accepts "0.15" or 15 and shows "15%" either way.

    Both spellings exist in this codebase's config and its provider payloads, and a rate printed as
    "0.15%" on a tax invoice is the kind of error that gets an invoice rejected rather than queried.
    """
    if rate is None or rate == "":
        # No rate is not a zero rate. Printing "0%" on a zero-rated or exempt supply asserts
        # something about the tax treatment that the data did not say.
        return ""
    try:
        value = float(rate)
    except (TypeError, ValueError):
        return ""
    if value <= 1:          # a fraction: 0.15
        value *= 100
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def line_vat(line: dict, *, tax_minor: int, taxable_minor: int) -> int:
    """This line's share of the invoice VAT, apportioned by its amount.

    Apportioned rather than recomputed per line, so the printed lines always sum to the invoice
    total that was actually charged. Recomputing invites a rounding difference between the sum of
    the lines and the total — and an invoice whose lines do not add up to its total is one a buyer
    is entitled to reject.
    """
    amount = int(line.get("amount_minor", 0) or 0)
    if taxable_minor <= 0 or tax_minor <= 0:
        return 0
    return round(tax_minor * amount / taxable_minor)


def apportion_vat(lines: list[dict], *, tax_minor: int, taxable_minor: int) -> list[int]:
    """Each line's VAT, with the rounding remainder given to the largest line.

    Rounding each line independently leaves the sum short or over by a unit or two. Rather than
    letting the document disagree with itself, the difference lands on the biggest line, where it
    is proportionally smallest and least surprising.
    """
    if not lines:
        return []
    shares = [line_vat(line, tax_minor=tax_minor, taxable_minor=taxable_minor) for line in lines]
    drift = tax_minor - sum(shares)
    if drift:
        biggest = max(range(len(lines)), key=lambda i: int(lines[i].get("amount_minor", 0) or 0))
        shares[biggest] += drift
    return shares


_MONTHS = ("January", "February", "March", "April", "May", "June",
           "July", "August", "September", "October", "November", "December")


def issued(value: str) -> str:
    """An ISO timestamp as a person reads it: "22 July 2026 · 09:14 UTC".

    ZATCA requires the issue date AND time on a tax invoice, so the time is kept rather than
    trimmed to a date. The raw ISO string was previously printed as-is; it is machine-correct and
    nobody reads `2026-07-22T09:14:00Z` off a document without pausing.

    Anything unparseable is returned untouched — a date this cannot read is still better on the
    page than a blank where a legally required field should be.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        date_part, _, time_part = raw.partition("T")
        y, m, d = (int(x) for x in date_part.split("-"))
        if not 1 <= m <= 12:
            return raw
        out = f"{d} {_MONTHS[m - 1]} {y}"
        hhmm = time_part[:5]
        if len(hhmm) == 5 and hhmm[2] == ":":
            out += f" · {hhmm} UTC" if raw.endswith("Z") else f" · {hhmm}"
        return out
    except (ValueError, IndexError):
        return raw
