"""WhatsAppProvider port — send one WhatsApp message.

WhatsApp is template-shaped, not free text: a business-initiated message must reference a template
Meta has APPROVED, with its variables substituted. So `send` carries the approved template NAME, its
language, and the ordered variables — never a rendered body the way email/SMS do. Returns True on
success, False on failure, exactly like the other channels' providers.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class WhatsAppProvider(Protocol):
    driver: str

    def send(self, to: str, template_name: str, language: str, variables: list[str]) -> bool: ...
