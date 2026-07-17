"""Shared value types exchanged across the stream ports.

Kept dependency-light (plain dataclasses) so every port and adapter can import them without a cycle.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class EngineSpec:
    """Everything an engine needs to ingest one source and produce the requested outputs."""
    source_url: str                 # resolved + IP-pinned by the caller (SSRF-safe)
    transport: str                  # tcp | udp | auto
    outputs: tuple[str, ...]        # subset of hls|webrtc|snapshot|thumbnail|record
    output_dir: str                 # where the engine writes HLS segments / recordings
    profile: dict[str, Any] = field(default_factory=dict)
    hls_segment_seconds: int = 2
    hls_window_segments: int = 5
    record_segment_seconds: int = 60
    record_format: str = "fmp4"
    has_audio: bool = True


@dataclass(frozen=True)
class EngineHandle:
    """An opaque reference to a running engine instance."""
    id: str
    pid: int | None = None
    output_dir: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EngineStats:
    fps: float = 0.0
    bitrate: int = 0
    bytes_out: int = 0
    frames: int = 0
    last_frame_at: str | None = None
    alive: bool = True


@dataclass(frozen=True)
class ProbeResult:
    codec: str = ""
    width: int = 0
    height: int = 0
    fps: float = 0.0
    has_audio: bool = False
    ptz_capable: bool = False
    ok: bool = True
    error: str = ""


@dataclass(frozen=True)
class Placement:
    """Where a session's engine runs, so the placer can find it again."""
    placer: str
    worker_id: str
    handle: EngineHandle


@dataclass(frozen=True)
class ReconcileResult:
    killed_orphans: tuple[str, ...] = ()      # engine ids with no owning session
    dead_sessions: tuple[str, ...] = ()       # session ids whose engine is gone


@dataclass(frozen=True)
class SegmentRef:
    """A recorded segment that now lives in the recording sink (e.g. a file-service object id)."""
    ref: str                                  # file_id / object key
    bytes: int
    start: str
    end: str


@dataclass(frozen=True)
class MotionEvent:
    session_id: str
    at: str
    score: float = 0.0


@dataclass(frozen=True)
class DiscoveredCamera:
    name: str
    url: str
    kind: str = "onvif"
    onvif_host: str = ""


@dataclass(frozen=True)
class PtzCapability:
    move: bool = False
    zoom: bool = False
    presets: tuple[dict[str, str], ...] = ()
