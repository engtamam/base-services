"""Null notifier — sends nothing, and reports that it sent nothing.

The zero-infrastructure default. It returns False rather than True, and that distinction is the
whole reason this file exists rather than a no-op: a null notifier reporting success would mean
verification emails were "sent" to everyone and received by no one, while every log line agreed
the flow worked. The boot guard refuses this driver in production when verification is enabled.

It records what it was asked to send, so a test can prove the send was ATTEMPTED — the difference
between "we asked and nothing was configured" and "we never asked at all".
"""
from __future__ import annotations

from typing import Any


class NullNotifier:
    driver = "null"

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, *, to: str, template: str, tenant: str,
                   variables: dict[str, Any] | None = None,
                   channel: str = "email") -> bool:
        self.sent.append({"to": to, "template": template, "tenant": tenant,
                          "variables": dict(variables or {}), "channel": channel})
        return False
