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


@dataclass
class Template:
    key: str
    locale: str
    subject: str
    body: str
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "locale": self.locale, "subject": self.subject,
                "body": self.body, "version": self.version}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Template":
        return cls(d["key"], d["locale"], d["subject"], d["body"], int(d.get("version", 1)))


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
    status: str            # sent | failed | suppressed
    provider: str = ""
    error: str = ""
    ts: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "recipient": self.recipient, "channel": self.channel,
                "template_key": self.template_key, "status": self.status,
                "provider": self.provider, "error": self.error, "ts": self.ts}


@dataclass
class InAppItem:
    id: str
    user: str
    title: str
    body: str
    read: bool = False
    ts: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "user": self.user, "title": self.title,
                "body": self.body, "read": self.read, "ts": self.ts}
