"""WhatsAppTemplateManager port — submit a template to Meta and read back its approval status.

Separate from the *sending* provider: a template is authored and approved once (a slow, human-gated
Meta review), then used for many sends. This port is that lifecycle side. The `console` driver
auto-approves for dev; the `meta` driver calls the Graph message_templates API on the WhatsApp
Business Account. Neither owns the template content — the adopter does; this only moves it through
Meta's approval and reports the verdict.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    provider_template_id: str = ""
    status: str = "pending"          # pending | approved | rejected
    reason: str = ""


@dataclass(frozen=True)
class StatusResult:
    status: str = "pending"          # pending | approved | rejected
    reason: str = ""


@runtime_checkable
class WhatsAppTemplateManager(Protocol):
    driver: str

    def submit(self, *, name: str, language: str, category: str, body: str,
               variables: list[str]) -> SubmitResult: ...

    def status(self, *, provider_template_id: str, name: str, language: str) -> StatusResult: ...
