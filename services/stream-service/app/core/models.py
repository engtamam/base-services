"""Domain models — Source, Session, Recording, Export, Trigger.

Credentials live sealed inside `Source.url_sealed`; the plaintext URL is never stored and never
serialized. `to_dict` is the storage shape; `public_dict` is what a client may see (url redacted,
no sealed blob, no internal placement).
"""
from __future__ import annotations

import datetime as _dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from app.core.sealing import redact_url


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def new_id() -> str:
    return uuid.uuid4().hex


# ── Session lifecycle ────────────────────────────────────────────────────────
STARTING, LIVE, RECONNECTING, STOPPING, STOPPED, ERROR = (
    "starting", "live", "reconnecting", "stopping", "stopped", "error")
_LIVE_STATES = frozenset({STARTING, LIVE, RECONNECTING})
_TERMINAL = frozenset({STOPPED, ERROR})

MODES = frozenset({"on_demand", "viewer_driven", "continuous", "scheduled"})
OUTPUTS = frozenset({"hls", "webrtc", "snapshot", "thumbnail", "record"})
STOP_REASONS = frozenset({"", "user", "max_duration", "idle", "max_size", "schedule", "error", "quota"})


@dataclass
class Source:
    id: str
    tenant: str
    name: str
    url_sealed: str                 # AES-GCM sealed rtsp/rtmp/http url (may embed credentials)
    url_host: str                   # redacted scheme://host, safe to show/log
    transport: str = "tcp"
    kind: str = "rtsp"
    onvif: dict[str, Any] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    group_id: str = ""
    enabled: bool = True
    default_profile: str = "default"
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "name": self.name,
                "url_sealed": self.url_sealed, "url_host": self.url_host,
                "transport": self.transport, "kind": self.kind, "onvif": self.onvif,
                "tags": self.tags, "group_id": self.group_id, "enabled": self.enabled,
                "default_profile": self.default_profile,
                "created": self.created, "updated": self.updated}

    def public_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "name": self.name,
                "url": self.url_host,          # redacted — never the sealed blob or credentials
                "transport": self.transport, "kind": self.kind, "onvif": self.onvif,
                "tags": self.tags, "group_id": self.group_id, "enabled": self.enabled,
                "default_profile": self.default_profile,
                "created": self.created, "updated": self.updated}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Source":
        return cls(id=d["id"], tenant=d.get("tenant", ""), name=d.get("name", ""),
                   url_sealed=d.get("url_sealed", ""),
                   url_host=d.get("url_host") or "", transport=d.get("transport", "tcp"),
                   kind=d.get("kind", "rtsp"), onvif=dict(d.get("onvif") or {}),
                   tags=list(d.get("tags") or []), group_id=d.get("group_id", ""),
                   enabled=bool(d.get("enabled", True)), default_profile=d.get("default_profile", "default"),
                   created=d.get("created", now()), updated=d.get("updated", now()))


@dataclass
class Guards:
    max_duration_s: int = 0
    idle_timeout_s: int = 0
    max_size_bytes: int = 0
    schedule: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"max_duration_s": self.max_duration_s, "idle_timeout_s": self.idle_timeout_s,
                "max_size_bytes": self.max_size_bytes, "schedule": self.schedule}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Guards":
        d = d or {}
        return cls(max_duration_s=int(d.get("max_duration_s") or 0),
                   idle_timeout_s=int(d.get("idle_timeout_s") or 0),
                   max_size_bytes=int(d.get("max_size_bytes") or 0),
                   schedule=dict(d.get("schedule") or {}))


@dataclass
class Session:
    id: str
    tenant: str
    source_id: str
    mode: str = "on_demand"
    status: str = STARTING
    outputs: list[str] = field(default_factory=lambda: ["hls"])
    profile: str = "default"
    guards: Guards = field(default_factory=Guards)
    placement: dict[str, Any] = field(default_factory=dict)   # internal — never public
    viewers: int = 0
    last_viewer_at: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    lease_expires_at: str = ""
    reason: str = ""
    started_at: str = ""
    created: str = field(default_factory=now)
    updated: str = field(default_factory=now)

    @property
    def is_live(self) -> bool:
        return self.status in _LIVE_STATES

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "source_id": self.source_id,
                "mode": self.mode, "status": self.status, "outputs": self.outputs,
                "profile": self.profile, "guards": self.guards.to_dict(),
                "placement": self.placement, "viewers": self.viewers,
                "last_viewer_at": self.last_viewer_at, "stats": self.stats,
                "lease_expires_at": self.lease_expires_at, "reason": self.reason,
                "started_at": self.started_at, "created": self.created, "updated": self.updated}

    def public_dict(self) -> dict[str, Any]:
        d = self.to_dict()
        d.pop("placement", None)          # how we find the process is not the client's business
        d.pop("last_viewer_at", None)
        d.pop("lease_expires_at", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        return cls(id=d["id"], tenant=d.get("tenant", ""), source_id=d.get("source_id", ""),
                   mode=d.get("mode", "on_demand"), status=d.get("status", STARTING),
                   outputs=list(d.get("outputs") or ["hls"]), profile=d.get("profile", "default"),
                   guards=Guards.from_dict(d.get("guards") or {}),
                   placement=dict(d.get("placement") or {}), viewers=int(d.get("viewers") or 0),
                   last_viewer_at=d.get("last_viewer_at", ""), stats=dict(d.get("stats") or {}),
                   lease_expires_at=d.get("lease_expires_at", ""), reason=d.get("reason", ""),
                   started_at=d.get("started_at", ""),
                   created=d.get("created", now()), updated=d.get("updated", now()))


@dataclass
class Recording:
    id: str
    tenant: str
    source_id: str
    session_id: str
    started: str
    ended: str = ""
    trigger: str = "continuous"          # continuous | motion | manual
    locked: bool = False                 # legal hold — exempt from retention
    total_bytes: int = 0
    segments: list[dict[str, Any]] = field(default_factory=list)
    created: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "source_id": self.source_id,
                "session_id": self.session_id, "started": self.started, "ended": self.ended,
                "trigger": self.trigger, "locked": self.locked, "total_bytes": self.total_bytes,
                "segments": self.segments, "created": self.created}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Recording":
        return cls(id=d["id"], tenant=d.get("tenant", ""), source_id=d.get("source_id", ""),
                   session_id=d.get("session_id", ""), started=d.get("started", now()),
                   ended=d.get("ended", ""), trigger=d.get("trigger", "continuous"),
                   locked=bool(d.get("locked", False)), total_bytes=int(d.get("total_bytes") or 0),
                   segments=list(d.get("segments") or []), created=d.get("created", now()))


@dataclass
class Export:
    id: str
    tenant: str
    source_id: str
    start: str
    end: str
    status: str = "pending"              # pending | ready | failed
    fmt: str = "mp4"
    file_id: str = ""
    created: str = field(default_factory=now)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "tenant": self.tenant, "source_id": self.source_id,
                "start": self.start, "end": self.end, "status": self.status,
                "fmt": self.fmt, "file_id": self.file_id, "created": self.created}

    def public_dict(self) -> dict[str, Any]:
        return {"id": self.id, "source_id": self.source_id, "start": self.start, "end": self.end,
                "status": self.status, "file_id": self.file_id or None}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Export":
        return cls(id=d["id"], tenant=d.get("tenant", ""), source_id=d.get("source_id", ""),
                   start=d.get("start", ""), end=d.get("end", ""), status=d.get("status", "pending"),
                   fmt=d.get("fmt", "mp4"), file_id=d.get("file_id", ""),
                   created=d.get("created", now()))
