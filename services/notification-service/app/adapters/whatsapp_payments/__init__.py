"""Native-payment adapter registry (build-ahead, KSA-dormant)."""
from __future__ import annotations

from app.adapters.whatsapp_payments.console import ConsoleWhatsAppPayments
from app.adapters.whatsapp_payments.meta import MetaWhatsAppPayments
from app.ports.whatsapp_payments import WhatsAppPayments

__all__ = ["ConsoleWhatsAppPayments", "MetaWhatsAppPayments", "WhatsAppPayments"]
