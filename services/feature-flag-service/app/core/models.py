"""Domain models — Flag, ConfigEntry/ConfigVersion, Maintenance."""
from __future__ import annotations

import datetime as _dt
from dataclasses import asdict, dataclass, field
from typing import Any


def now_ts() -> float:
    return _dt.datetime.now(_dt.timezone.utc).timestamp()


def iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


@dataclass
class Flag:
    key: str
    description: str = ""
    enabled: bool = False                 # master switch / kill-switch
    default: bool = False                 # answer when nothing else matches
    rollout_pct: float = 100.0
    bucket_by: str = "user"               # user | tenant | attr:<name> — WHAT the rollout hashes
    variants: dict[str, float] = field(default_factory=dict)   # {name: weight} summing to 100
    rules: list[dict] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)          # reusable named audiences
    prerequisites: list[dict] = field(default_factory=list)    # [{key, variant?}]
    tenant_overrides: dict[str, bool] = field(default_factory=dict)
    user_overrides: dict[str, bool] = field(default_factory=dict)
    requires_entitlement: str = ""         # plan entitlement key (billing-service)
    start: float = 0.0                     # scheduled window (0 = always)
    end: float = 0.0
    archived: bool = False
    created: str = ""
    last_evaluated: float = 0.0            # drives stale-flag cleanup

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Flag":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Segment:
    """A named, reusable audience — defined once, referenced by many flags."""
    key: str
    description: str = ""
    rules: list[dict] = field(default_factory=list)     # any rule matching → in segment
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Segment":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class ConfigEntry:
    key: str
    value: Any = None
    version: int = 0
    env: str = ""                          # "" = global; an env-scoped entry wins over global
    actor: str = ""
    created: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ConfigEntry":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})


@dataclass
class Maintenance:
    module: str = "global"                 # "global" or a module name (partial maintenance)
    enabled: bool = False
    message: str = ""
    start: float = 0.0                     # 0 = immediately
    end: float = 0.0                       # 0 = until turned off
    updated: str = ""

    def active_now(self, now: float | None = None) -> bool:
        """A scheduled window: active only between start and end (when set)."""
        if not self.enabled:
            return False
        t = now_ts() if now is None else now
        if self.start and t < self.start:
            return False
        if self.end and t >= self.end:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Maintenance":
        return cls(**{k: d.get(k, getattr(cls, k, None)) for k in cls.__dataclass_fields__})
