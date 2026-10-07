"""The domain events billing publishes.

Until now this service recorded money correctly and told nobody. Every integration an adopter still
had to write — credit a wallet on payment, react to a renewal, warn before a balance runs out — was
a place the plug-and-play promise stopped being true, and each one got written differently by every
adopter.

WHAT AN EVENT IS HERE, AND WHAT IT IS NOT. The event bus is at-most-once and best effort; there is
no outbox, no retry and no replay (see `ports/event_bus.py`). An event therefore tells any consumer
that something happened so they can react WITHOUT this service knowing they exist. It is a hint.

It is NOT how a money consequence is delivered. A subscriber that is restarting when the event fires
never receives it and nothing records that it missed one — so crediting a wallet purely from a
subscription would mean a customer pays and their credits never arrive. Not late: never, because
nothing will ever say it again. Anything that MUST happen goes through a durable, idempotent path
and reads the fact back; the event only makes that path faster.

PUBLISHING NEVER BREAKS THE THING THAT HAPPENED, by default. The money has already moved by the
time these fire, and refusing to record a payment because a message broker is unreachable turns a
degraded notification path into a failed payment. `EVENT_PUBLISH_FAILURE=fail_closed` is available
for platforms that would rather refuse than proceed unobserved — but it is not the default, because
here the alternative to a missed hint is a lost payment.
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from app.ports.event_bus import Event

_log = logging.getLogger(__name__)

# Declared here and in the manifest, and the two must agree — an event a consumer cannot discover
# is one nobody subscribes to, which is the same as not publishing it.
PAYMENT_SUCCEEDED = "payment.succeeded"
PAYMENT_FAILED = "payment.failed"
PAYMENT_REFUNDED = "payment.refunded"
SUBSCRIPTION_RENEWED = "subscription.renewed"
SUBSCRIPTION_CANCELLED = "subscription.cancelled"
INVOICE_PAID = "invoice.paid"
# A new invoice has been raised for a subscription and is NOT yet paid — fired the moment it becomes
# due, BEFORE any charge attempt or dunning. DISTINCT from invoice.paid (which fires when the money
# lands): this is the "awaiting payment" hint a consumer uses to send a pay-link / reminder. A hint,
# never money — the invoice is the durable fact (read it back via GET /internal/billing/invoices/{id}).
INVOICE_CREATED = "invoice.created"
# A subscription's current period ends within the configured warning window (SUBSCRIPTION_EXPIRY_WARN
# _DAYS). Fired EXACTLY ONCE per period by the daily expiry-warn sweep (idempotent per
# subscription+period_end), and re-arms naturally when the period rolls forward on renewal. A proactive
# "renews/expires soon" prompt — distinct from subscription.renewed, which fires AFTER the roll.
SUBSCRIPTION_EXPIRING_SOON = "subscription.expiring_soon"
# A wallet-renewal debit was refused because the customer's fund could not cover the invoice. The
# subscription is suspended IMMEDIATELY (no dunning retry — a prepaid balance does not grow back on
# its own), so a consumer sends a "top up your wallet" prompt. Distinct from payment.failed, which
# is a card decline that WILL be retried.
SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT = "subscription.renewal_wallet_insufficient"
# A subscription LAPSED into past_due — a card renewal that failed (dunning, will retry) or a wallet
# renewal that could not be covered — so its entitlements stop. Emitted once, on the TRANSITION into
# past_due, so a consumer can react instantly (e.g. disable a lapsed feature) instead of polling.
# `reason` distinguishes the cause. The return direction (past_due -> active on payment) is already
# SUBSCRIPTION_RENEWED; a terminal stop is SUBSCRIPTION_CANCELLED.
SUBSCRIPTION_SUSPENDED = "subscription.suspended"
# A renewal was invoiced on a gateway with NO saved-card charge (a hosted-checkout-only gateway such
# as TuwaiqPay): there is no token to charge, so instead of a doomed card retry the service mints a
# hosted PAY LINK for the renewal invoice and emits this so a channel (WhatsApp/email/in-app) delivers
# it to the payer. The invoice stays open and is settled through the normal webhook/reconcile seam
# when the payer pays; if unpaid past grace the subscription lapses like any other unpaid renewal.
SUBSCRIPTION_RENEWAL_PAYMENT_LINK = "subscription.renewal_payment_link"

EMITTED = (
    PAYMENT_SUCCEEDED, PAYMENT_FAILED, PAYMENT_REFUNDED,
    SUBSCRIPTION_RENEWED, SUBSCRIPTION_CANCELLED, INVOICE_PAID, INVOICE_CREATED,
    SUBSCRIPTION_EXPIRING_SOON,
    SUBSCRIPTION_RENEWAL_WALLET_INSUFFICIENT, SUBSCRIPTION_SUSPENDED,
    SUBSCRIPTION_RENEWAL_PAYMENT_LINK,
)


class Emitter:
    """Publishes domain events, and decides what happens when the bus cannot be reached."""

    def __init__(self, bus, *, fail_closed: bool = False, clock=None) -> None:
        self._bus = bus                  # None when no bus is wired: publishing is then a no-op
        self._fail_closed = fail_closed
        self._clock = clock

    async def emit(self, event_type: str, payload: dict[str, Any], *,
                   correlation_id: str = "") -> None:
        if self._bus is None:
            return
        if event_type not in EMITTED:
            # An undeclared event is one no consumer can discover from the manifest, so it reaches
            # nobody by design. Refusing here keeps the declaration and the code in step rather
            # than letting them drift until someone reads both.
            raise ValueError(
                f"{event_type!r} is not declared in EMITTED — declare it there and in the "
                f"manifest, or no consumer can discover it")
        event = Event(
            type=event_type,
            payload=payload,
            id=str(uuid.uuid4()),
            correlation_id=correlation_id,
            ts=self._clock() if self._clock else "",
        )
        try:
            await self._bus.publish(event_type, event)
        except Exception as exc:  # noqa: BLE001 — any broker failure, and there are many
            if self._fail_closed:
                raise
            # The money already moved. Refusing to record a payment because a broker is
            # unreachable turns a degraded notification path into a failed payment, and the
            # consumer's own reconciliation is what makes a missed hint survivable.
            _log.warning("event_publish_failed",
                         extra={"event": event_type, "error": type(exc).__name__})


def payment_payload(inv, *, payment_id: str = "", source: str = "") -> dict[str, Any]:
    """What a consumer needs to act, and nothing that is not theirs to hold.

    Carries the PAYMENT IDENTITY, because that is what a consumer keys its idempotency on: a
    redelivery and a genuine second payment of the same amount are otherwise indistinguishable, and
    crediting twice is money given away.

    No buyer name, address or tax number. A payload lands in logs and in every subscriber's store —
    including subscribers that have no lawful basis to hold a person's details — and the tenant plus
    the invoice id are enough to look up anything more from a service that is allowed to.
    """
    return {
        "tenant": inv.tenant,
        "invoice_id": inv.id,
        "payment_id": payment_id or inv.id,
        "amount_minor": inv.total_minor,
        "currency": inv.currency,
        "paid_at": getattr(inv, "paid_at", "") or "",
        "source": source,
    }


def invoice_created_payload(inv, *, subscription_id: str = "", plan_id: str = "",
                            period_start: Any = "", period_end: Any = "") -> dict[str, Any]:
    """What a consumer needs when an invoice is raised and is awaiting payment.

    The SAME privacy discipline as payment_payload(): tenant + invoice id + the amount, and nothing
    that is not the consumer's to hold (no buyer name/address/tax number). `due_at` is the invoice's
    own creation timestamp — the moment it became due — and period_start/period_end carry the billing
    window the invoice covers (the due-date proxy billing already uses on the invoice meta). For a
    CONSOLIDATED invoice there is no single subscription, so subscription_id/plan_id are empty and the
    invoice id is the thing to key on.
    """
    return {
        "tenant": inv.tenant,
        "subscription_id": subscription_id or (getattr(inv, "subscription_id", "") or ""),
        "invoice_id": inv.id,
        "plan_id": plan_id,
        "amount_minor": inv.total_minor,
        "currency": inv.currency,
        "due_at": getattr(inv, "created", "") or "",
        "period_start": period_start,
        "period_end": period_end,
    }
