"""WhatsAppFlowManager port — create / clone / publish / list WhatsApp Flows on a Business Account.

A WhatsApp Flow (the form a template's flow-button opens) is WABA-SCOPED, exactly like a template: it
is created with POST /{waba_id}/flows and lives under that one WABA. There is no global Flow many
accounts can point at — Meta's own multi-WABA story is CLONE (a copy with the SAME name and a NEW id
in the destination WABA). So in a model where every tenant connects its own WABA, each tenant needs
its own Flow, and templates should reference it by flow_NAME (stable across clones), never flow_id
(different per clone).

This port is the lifecycle side, the twin of WhatsAppTemplateManager: it moves a Flow through Meta's
create → publish, and lists what a WABA already has so provisioning is IDEMPOTENT BY NAME (a re-run
finds the existing Flow instead of creating a duplicate — which matters because a PUBLISHED Flow is
immutable and cannot be deleted). The `console` driver auto-succeeds for dev; the `meta` driver calls
the Graph Flows API. Neither owns the flow_json — the adopter authors it once and passes it (or a
clone_flow_id) in; this only provisions and reports.

Verified against Meta's WhatsApp Flows API reference (Sep 2026): create is POST /{waba_id}/flows with
{name, categories, flow_json | clone_flow_id, endpoint_uri?, publish?}; publish is POST
/{flow_id}/publish; list is GET /{waba_id}/flows returning {id, name, status, categories} per flow;
status ∈ DRAFT | PUBLISHED | DEPRECATED | BLOCKED | THROTTLED; categories are drawn from Meta's fixed
set (SIGN_UP, SIGN_IN, APPOINTMENT_BOOKING, LEAD_GENERATION, CONTACT_US, CUSTOMER_SUPPORT, SURVEY,
OTHER).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Meta's fixed category set. A category outside it is refused locally rather than sent and 400'd.
FLOW_CATEGORIES = ("SIGN_UP", "SIGN_IN", "APPOINTMENT_BOOKING", "LEAD_GENERATION",
                   "CONTACT_US", "CUSTOMER_SUPPORT", "SURVEY", "OTHER")

DRAFT, PUBLISHED, DEPRECATED, BLOCKED, THROTTLED = (
    "DRAFT", "PUBLISHED", "DEPRECATED", "BLOCKED", "THROTTLED")


@dataclass(frozen=True)
class FlowSummary:
    """One row of a WABA's Flow list — the identity + state a provisioner reasons about."""
    id: str
    name: str
    status: str = DRAFT
    categories: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlowResult:
    ok: bool
    flow_id: str = ""
    name: str = ""
    status: str = DRAFT
    reason: str = ""
    validation_errors: tuple[str, ...] = field(default_factory=tuple)


@runtime_checkable
class WhatsAppFlowManager(Protocol):
    driver: str

    def list_flows(self) -> list[FlowSummary]:
        """Every Flow on the WABA (id, name, status, categories). Empty on an unreachable WABA —
        the provisioner treats 'cannot list' as 'do not blindly create', never as 'none exist'."""
        ...

    def create(self, *, name: str, categories: list[str], flow_json: str = "",
               clone_flow_id: str = "", endpoint_uri: str = "",
               publish: bool = False) -> FlowResult:
        """Create a Flow under the WABA — from flow_json, or as a clone of clone_flow_id (a copy with
        this name and a new id). `publish` asks Meta to publish in the same call where supported."""
        ...

    def publish(self, *, flow_id: str) -> FlowResult:
        """Publish a DRAFT Flow (POST /{flow_id}/publish). A published Flow is immutable thereafter."""
        ...
