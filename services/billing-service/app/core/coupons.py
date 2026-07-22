"""Coupons: whether one may be used, and what an operator is shown about it.

THREE THINGS WERE DECORATIVE HERE, and all three cost money.

`max_redemptions` was never enforced. Nothing in the service ever incremented `redeemed` — the
field existed on the model and no line of code wrote it. So a coupon capped at a hundred uses
worked for ever, and an operator who set that cap believed a limit existed that did not.

`expires_at` was never checked. `coupon_discount` looked at `active` and nothing else, so a
campaign that ended in March kept discounting in July.

And neither could be set anyway: the request model accepted only code, type, value and
max_redemptions, so `expires_at` and `active` were unreachable through the API and reset to their
defaults on every write. A panel that offered "turn this coupon off" was offering something the
service could not do.

WHY REDEMPTION IS COUNTED AT SIGNUP, not per billing cycle. `max_redemptions` means "how many
customers may use this", not "how many invoices it may touch". Counting per cycle would make a
monthly coupon consume its own cap every month and expire itself after N months for one customer.
The claim happens once, when the code is attached to a subscription.

WHY STATUS IS DERIVED AND NOT STORED. An operator looking at a coupon that is not discounting needs
to know WHY: not started, expired, used up, or switched off. Storing a status means something has
to remember to update it when a clock passes a timestamp, and nothing ever does — so it is computed
from the facts every time it is asked for.
"""
from __future__ import annotations

from typing import Any

# What an operator sees. Ordered by precedence: the first true reason is the one shown, because
# "expired AND exhausted" is not a more useful answer than "expired".
UNKNOWN = "unknown"          # no such code
DISABLED = "disabled"        # switched off by hand
SCHEDULED = "scheduled"      # starts_at is in the future
EXPIRED = "expired"          # expires_at has passed
EXHAUSTED = "exhausted"      # redeemed >= max_redemptions
ACTIVE = "active"

STATUSES = (UNKNOWN, DISABLED, SCHEDULED, EXPIRED, EXHAUSTED, ACTIVE)


def status(coupon: dict[str, Any] | None, now: float) -> str:
    """Why this coupon is or is not usable, right now."""
    # `is None` and not a truthiness test: an empty dict is a coupon with default values, not an
    # absent one, and conflating them reported a brand-new coupon as expired. A missing code gets
    # its own answer because "there is no such code" and "that campaign ended" are different
    # things to tell a customer.
    if coupon is None:
        return UNKNOWN
    if not coupon.get("active", True):
        return DISABLED
    starts = coupon.get("starts_at")
    if starts and float(starts) > now:
        return SCHEDULED
    expires = coupon.get("expires_at")
    if expires and float(expires) <= now:
        return EXPIRED
    cap = int(coupon.get("max_redemptions", 0) or 0)
    if cap and int(coupon.get("redeemed", 0) or 0) >= cap:
        return EXHAUSTED
    return ACTIVE


def usable(coupon: dict[str, Any] | None, now: float) -> bool:
    return status(coupon, now) == ACTIVE


def remaining(coupon: dict[str, Any] | None) -> int | None:
    """Redemptions left, or None when the coupon is uncapped.

    None rather than a large number: a UI that renders "unlimited" and one that renders "999999"
    are telling an operator different things, and only one of them is true.
    """
    if coupon is None:
        return 0
    cap = int(coupon.get("max_redemptions", 0) or 0)
    if not cap:
        return None
    return max(0, cap - int(coupon.get("redeemed", 0) or 0))


def view(coupon: dict[str, Any], now: float) -> dict[str, Any]:
    """A coupon as an operator should see it: the stored facts plus why it behaves as it does."""
    out = dict(coupon)
    out["status"] = status(coupon, now)
    out["remaining"] = remaining(coupon)
    return out
