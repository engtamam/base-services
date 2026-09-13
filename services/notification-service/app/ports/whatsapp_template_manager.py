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
from typing import Any, Protocol, runtime_checkable


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


@dataclass(frozen=True)
class MessagingLimit:
    """A phone number's business-initiated messaging ceiling, read from Meta.

    Meta caps business-initiated conversations at a number of UNIQUE customers per rolling 24 hours,
    at the PORTFOLIO level (shared across a WABA's numbers), and moves the tier automatically as the
    account's volume and quality change. `limit` is that ceiling as an integer (-1 = unlimited,
    0 = unknown); `tier` is Meta's raw code (e.g. "TIER_250") kept verbatim so a new tier we have
    not mapped is never silently lost. `quality_rating` (GREEN/YELLOW/RED) predicts a drop before it
    happens. A caller reserves capacity and plans a campaign against `limit`; without it, both are a
    guess about Meta's system."""
    ok: bool
    limit: int = 0                   # unique customers / 24h; -1 unlimited, 0 unknown
    tier: str = ""                   # Meta's raw tier code, e.g. "TIER_250"
    quality_rating: str = ""         # GREEN | YELLOW | RED | NA | UNKNOWN
    display_phone_number: str = ""
    verified_name: str = ""
    name_status: str = ""
    error: str = ""

    def to_dict(self) -> dict:
        return {"ok": self.ok, "limit": self.limit, "tier": self.tier,
                "quality_rating": self.quality_rating,
                "display_phone_number": self.display_phone_number,
                "verified_name": self.verified_name, "name_status": self.name_status,
                "error": self.error}


@runtime_checkable
class WhatsAppTemplateManager(Protocol):
    driver: str

    def submit(self, *, name: str, language: str, category: str, body: str,
               variables: list[str], examples: list[str],
               flow_button: dict[str, Any] | None = None,
               carousel: dict[str, Any] | None = None,
               footer: str = "",
               header: dict[str, Any] | None = None,
               button: dict[str, Any] | None = None) -> SubmitResult: ...

    def status(self, *, provider_template_id: str, name: str, language: str) -> StatusResult: ...

    def messaging_limit(self, *, phone_number_id: str) -> MessagingLimit:
        # OPTIONAL: read the phone number's current messaging limit + quality rating from Meta. Only
        # a driver that can talk to Meta implements it; the route reports 'unsupported' otherwise —
        # it never fabricates a ceiling, because a made-up limit is worse than a known-unknown here.
        ...
