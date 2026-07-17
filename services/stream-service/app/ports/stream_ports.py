"""The stream-service ports (Protocols). Every live capability is one of these, so a null adapter
lets the whole service boot and be tested with zero infra, and a customer swaps any one without
touching the core.

  StreamEngine    run the ingest+encode process for one source; produce outputs; report stats
  SessionPlacer   decide WHERE a session runs (this pod | a worker) and own its lease/reconcile
  PtzController   move a physical camera (ONVIF)
  MotionDetector  surface motion events from a running session
  Discovery       find cameras on a network
  RecordingSink   store a recorded segment (file-service | s3 | local)
  SignedPlayback  mint/verify short-lived, scoped playback tokens
  MediaEgress     WebRTC ICE/TURN configuration
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from app.ports.stream_types import (
    DiscoveredCamera, EngineHandle, EngineSpec, EngineStats, MotionEvent, Placement,
    ProbeResult, PtzCapability, ReconcileResult, SegmentRef,
)


@runtime_checkable
class StreamEngine(Protocol):
    driver: str

    async def start(self, spec: EngineSpec) -> EngineHandle: ...
    async def stop(self, handle: EngineHandle) -> None: ...
    async def stats(self, handle: EngineHandle) -> EngineStats: ...
    async def is_alive(self, handle: EngineHandle) -> bool: ...
    async def snapshot(self, source_url: str, *, transport: str = "tcp",
                       max_width: int = 1280) -> bytes: ...
    async def probe(self, source_url: str, *, transport: str = "tcp") -> ProbeResult: ...


@runtime_checkable
class SessionPlacer(Protocol):
    driver: str

    async def start(self, session_id: str, spec: EngineSpec) -> Placement:
        """Place and start the session's engine; return where it landed."""
        ...

    async def stop(self, placement: Placement) -> None: ...
    async def stats(self, placement: Placement) -> EngineStats: ...
    async def is_alive(self, placement: Placement) -> bool: ...

    async def reconcile(self, known_session_ids: set[str]) -> ReconcileResult:
        """Kill any engine with no owning session; report any session whose engine is gone. The
        answer to zombie processes: neither a process nor a session may outlive the other."""
        ...


@runtime_checkable
class PtzController(Protocol):
    driver: str

    async def capability(self, source: dict[str, Any]) -> PtzCapability: ...
    async def move(self, source: dict[str, Any], *, pan: float, tilt: float, zoom: float,
                   mode: str = "continuous") -> None: ...
    async def stop(self, source: dict[str, Any]) -> None: ...
    async def presets(self, source: dict[str, Any]) -> list[dict[str, str]]: ...
    async def goto(self, source: dict[str, Any], token: str) -> None: ...


@runtime_checkable
class MotionDetector(Protocol):
    driver: str

    async def poll(self, session_id: str, handle: EngineHandle) -> list[MotionEvent]:
        """Return motion events observed since the last poll (empty if none / unsupported)."""
        ...


@runtime_checkable
class Discovery(Protocol):
    driver: str

    async def discover(self, timeout_seconds: float = 3.0) -> list[DiscoveredCamera]: ...


@runtime_checkable
class RecordingSink(Protocol):
    driver: str

    async def put_segment(self, *, tenant: str, session_id: str, seq: int, data: bytes,
                          content_type: str, start: str, end: str) -> SegmentRef: ...
    async def delete(self, ref: str) -> bool: ...
    async def signed_url(self, ref: str, *, ttl: int) -> str: ...


@runtime_checkable
class SignedPlayback(Protocol):
    driver: str

    def sign(self, *, tenant: str, session_id: str, output: str, ttl: int) -> str: ...
    def verify(self, token: str) -> dict[str, Any] | None: ...


@runtime_checkable
class MediaEgress(Protocol):
    driver: str

    def ice_servers(self) -> list[dict[str, Any]]: ...
