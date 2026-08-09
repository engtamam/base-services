"""WhatsAppTemplateManager port — submit a template to Meta and read back its approval status.

Separate from the *sending* provider: a template is authored and approved once (a slow, human-gated
Meta review), then used for many sends. This port is that lifecycle side. The `console` driver
auto-approves for dev; the `meta` driver calls the Graph message_templates API on the WhatsApp
Business Account. Neither owns the template content — the adopter does; this only moves it through
Meta's approval and reports the verdict.

TWO ORDERED LISTS, AND THEY ARE NOT THE SAME THING. A body carries positional placeholders
({{1}}, {{2}}, …). `variables` are the ordered parameter NAMES — the keys a send's `variables`
dict is keyed by, whose ORDER maps name→position — so the send path can substitute by name while
Meta fills by position. `examples` are the ordered EXAMPLE VALUES, one per variable, that Meta
REQUIRES at creation: "Upon template creation, you must include an example value for each
parameter." A variable NAME (e.g. "customer_name") is not a valid example — Meta's review rejects
placeholder-looking examples — so the two must be distinct. When `examples` is short or absent the
meta driver falls back to the variable name per slot, which is a weak example but a submittable one;
the value that ships is always the caller's at send time, never the example.

STATUS VOCABULARY (normalised from Meta's, which has grown): pending | approved | rejected | paused
| disabled. `paused` and `disabled` are distinct from `rejected` on purpose — Meta pauses a template
for recurring negative feedback or low read-rates and it can COME BACK; it disables one that never
does; a rejected one cannot be resubmitted under the same name. Folding any of these into `pending`
would tell an operator to wait for a verdict that will never change.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class SubmitResult:
    ok: bool
    provider_template_id: str = ""
    status: str = "pending"          # pending | approved | rejected | paused | disabled
    reason: str = ""


@dataclass(frozen=True)
class StatusResult:
    status: str = "pending"          # pending | approved | rejected | paused | disabled
    reason: str = ""


@runtime_checkable
class WhatsAppTemplateManager(Protocol):
    driver: str

    def submit(self, *, name: str, language: str, category: str, body: str,
               variables: list[str], examples: list[str]) -> SubmitResult: ...

    def status(self, *, provider_template_id: str, name: str, language: str) -> StatusResult: ...
