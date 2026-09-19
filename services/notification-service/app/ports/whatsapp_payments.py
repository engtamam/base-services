"""WhatsAppPayments port — native in-WhatsApp payment lookup + refund (build-ahead, KSA-dormant).

The order_details "Review and Pay" MESSAGE is sent through the ordinary interactive send path (see
core/whatsapp_interactive.build_order_details). This port is the OTHER half: the server-to-server
Graph calls that a payment webhook must be checked against and that issue refunds — the parts that
carry money and therefore must never be trusted from a webhook body alone.

  * get_payment_status — GET /{phone_number_id}/payments/{configuration}/{reference_id}: the source of
    truth. A `type:"payment"` webhook is only a HINT; the real status is what THIS returns.
  * refund           — POST /{phone_number_id}/payments_refund.

Native pay via a gateway is India-only today, so every implementation is gated upstream by
WHATSAPP_NATIVE_PAY_ENABLED — this port is the shape the seam will use the day KSA is enabled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class PaymentLookup:
    ok: bool
    reference_id: str = ""
    status: str = ""                      # captured | pending | failed | canceled | unknown
    amount_minor: int = 0
    offset: int = 100
    currency: str = ""
    transaction_id: str = ""
    verified: bool = False                # True only when the Graph lookup actually answered
    error: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RefundResult:
    ok: bool
    refund_id: str = ""
    status: str = ""
    error: str = ""


@runtime_checkable
class WhatsAppPayments(Protocol):
    driver: str

    def get_payment_status(self, *, configuration: str, reference_id: str) -> PaymentLookup: ...

    def refund(self, *, reference_id: str, configuration: str, amount_minor: int, currency: str,
               offset: int = 100, speed: str = "normal") -> RefundResult: ...
