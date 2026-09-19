"""Console native-payment adapter — deterministic, no network. The zero-infra default + test double.
Lets the whole native-pay seam (lookup-verify, refund) be exercised without Meta or a live gateway."""
from __future__ import annotations

from app.ports.whatsapp_payments import PaymentLookup, RefundResult


class ConsoleWhatsAppPayments:
    driver = "console"

    def __init__(self, *, status: str = "captured", amount_minor: int = 0, currency: str = "SAR",
                 offset: int = 100, **_) -> None:
        self._status, self._amount, self._currency, self._offset = status, amount_minor, currency, offset

    def get_payment_status(self, *, configuration: str, reference_id: str) -> PaymentLookup:
        return PaymentLookup(ok=True, reference_id=reference_id, status=self._status,
                             amount_minor=self._amount, offset=self._offset, currency=self._currency,
                             transaction_id=f"console_tx_{reference_id}", verified=True)

    def refund(self, *, reference_id: str, configuration: str, amount_minor: int, currency: str,
               offset: int = 100, speed: str = "normal") -> RefundResult:
        return RefundResult(ok=True, refund_id=f"console_refund_{reference_id}", status="pending")
