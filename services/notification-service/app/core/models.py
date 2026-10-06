"""Domain models — generic (no product-specific fields)."""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# A template's CONSENT CATEGORY — the source of truth for "does sending this need consent?". A
# `marketing` template MUST be mapped to a consent purpose (NOTIFY_CONSENT_PURPOSES) so the send path
# gates it; the boot + write guards enforce that, closing the gap where a marketing template a
# developer forgot to map shipped as unchecked "transactional". Default transactional (OTP, receipts,
# booking updates — sent under contract, not consent).
TEMPLATE_TRANSACTIONAL = "transactional"
TEMPLATE_MARKETING = "marketing"
TEMPLATE_CATEGORIES = (TEMPLATE_TRANSACTIONAL, TEMPLATE_MARKETING)


@dataclass
class Template:
    key: str
    locale: str
    subject: str
    body: str
    version: int = 1
    category: str = TEMPLATE_TRANSACTIONAL

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "locale": self.locale, "subject": self.subject,
                "body": self.body, "version": self.version, "category": self.category}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Template":
        return cls(d["key"], d["locale"], d["subject"], d["body"], int(d.get("version", 1)),
                   str(d.get("category", TEMPLATE_TRANSACTIONAL) or TEMPLATE_TRANSACTIONAL))


@dataclass
class Preference:
    user: str
    suppressed: bool = False               # global opt-out (unsubscribe)
    opt_out_channels: list[str] = field(default_factory=list)   # e.g. ["sms"]
    opt_out_events: list[str] = field(default_factory=list)     # e.g. ["marketing"]

    def allows(self, channel: str, event: str | None) -> bool:
        if self.suppressed:
            return False
        if channel in self.opt_out_channels:
            return False
        if event and event in self.opt_out_events:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {"user": self.user, "suppressed": self.suppressed,
                "opt_out_channels": self.opt_out_channels, "opt_out_events": self.opt_out_events}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Preference":
        return cls(d["user"], bool(d.get("suppressed", False)),
                   list(d.get("opt_out_channels") or []), list(d.get("opt_out_events") or []))


@dataclass
class Delivery:
    id: str
    recipient: str
    channel: str
    template_key: str
    # queued | sending | sent | suppressed | failed | dead_letter
    #
    # `failed` and `dead_letter` are both terminal and deliberately NOT the same thing:
    #   failed       asking again would fail identically (bad address, missing template)
    #   dead_letter  it could have worked, we retried, we ran out of patience
    # Collapsing them hides which outages actually cost us messages.
    status: str
    # The tenant this send belongs to, populated at send time (the caller knows it). Empty for a send
    # with no tenant context. It is what lets an operator ask "show me THIS shop's deliveries" — a
    # WhatsApp diagnostic screen filters on it — without a phone_number_id→tenant guess after the fact.
    tenant: str = ""
    provider: str = ""
    error: str = ""
    attempts: int = 0                  # how many sends were tried
    next_attempt_ts: float = 0.0       # when the next one may run (backoff + full jitter)
    payload: dict[str, Any] = field(default_factory=dict)   # what a worker needs to re-send
    ts: str = field(default_factory=now)
    # The provider's own message id (WhatsApp's wamid). The ONLY join key between this send and the
    # delivery/read receipt that arrives later by webhook — the receipt echoes the identical value.
    provider_message_id: str = ""
    # The receipt axis, DISTINCT from our send-state machine above (queued/sent/failed/…): what Meta
    # last told us HAPPENED to the message — "" | sent | delivered | read | failed — and WHEN. The ts
    # is the watermark a consumer reads deliveries "changed after" to catch up on missed bus events.
    receipt_status: str = ""
    receipt_ts: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "recipient": self.recipient, "channel": self.channel,
                "tenant": self.tenant,
                "template_key": self.template_key, "status": self.status,
                "provider": self.provider, "error": self.error,
                "attempts": self.attempts, "next_attempt_ts": self.next_attempt_ts,
                "payload": self.payload, "ts": self.ts,
                "provider_message_id": self.provider_message_id,
                "receipt_status": self.receipt_status, "receipt_ts": self.receipt_ts}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Delivery":
        return cls(
            id=d["id"], recipient=d["recipient"], channel=d["channel"],
            tenant=d.get("tenant", ""),
            template_key=d["template_key"], status=d["status"],
            provider=d.get("provider", ""), error=d.get("error", ""),
            attempts=int(d.get("attempts", 0)),
            next_attempt_ts=float(d.get("next_attempt_ts", 0.0)),
            payload=dict(d.get("payload") or {}),
            ts=d.get("ts", now()),
            provider_message_id=d.get("provider_message_id", ""),
            receipt_status=d.get("receipt_status", ""),
            receipt_ts=float(d.get("receipt_ts", 0.0)),
        )


@dataclass
class InAppItem:
    id: str
    user: str
    title: str
    body: str
    read: bool = False
    ts: str = field(default_factory=now)
    # An OPTIONAL internal deep link — a relative in-app path the console/mobile bell navigates to when
    # the item is tapped (e.g. "/whatsapp-orders/102"), so a "new order" notification opens that order
    # instead of the home screen. Empty = no target (today's behaviour; the bell only marks-as-read).
    # It is a RELATIVE path by contract (validated at the API edge), never an absolute/external URL, so a
    # trusted notification can never deep-link a tenant to an attacker-controlled site.
    deep_link: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "user": self.user, "title": self.title,
                "body": self.body, "read": self.read, "ts": self.ts,
                "deep_link": self.deep_link}
