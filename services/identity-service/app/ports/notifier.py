"""Notifier port — how identity-service asks for a message to reach a human.

This service had no way to send anything. That is why self-service registration could not prove
an address: proving it requires sending something only the owner can read, and there was no wire
out of here.

DELIBERATELY THIN. identity-service does not render templates, choose channels, or know about
providers — notification-service owns all of that. This port carries an intent (`send this
template, to this address, with these variables`) and nothing else. Anything richer would put a
second copy of notification's job in a service that must stay small enough to reason about.

FAILURE IS NOT SILENT AND NOT FATAL. `send` never raises: a mail provider hiccup must not turn
registration into a 500 for a user whose account was created perfectly well. It returns whether
the message was accepted, and the caller decides — for verification, the account still exists and
the human can ask for a resend, which is a far better outcome than a failed signup.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    driver: str

    async def send(self, *, to: str, template: str, tenant: str,
                   variables: dict[str, Any] | None = None,
                   channel: str = "email") -> bool:
        """True if accepted for delivery. Never raises.

        `channel` selects HOW it reaches the human — email / sms / whatsapp — and is the real
        selector notification-service routes on. It defaults to email so the address-verification
        path (which only ever mails) is unchanged; the OTP path passes OTP_DELIVERY_CHANNEL so a
        phone OTP can go over WhatsApp instead of email.
        """
        ...
