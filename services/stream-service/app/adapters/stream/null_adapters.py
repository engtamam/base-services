"""Null / none adapters for the optional stream ports — the service boots with zero infra.

Each is a HONEST no-op: it never pretends work happened. NullPtz reports "no PTZ" (a move raises via
the core's capability check, not here); NoneMotion never sees motion; NoneDiscovery finds nothing;
DirectMediaEgress offers no TURN (host/srflx only). The real adapters (onvif, ffmpeg-scene, turn) are
drop-ins.
"""
from __future__ import annotations

from typing import Any

from app.ports.stream_types import DiscoveredCamera, EngineHandle, MotionEvent, PtzCapability


class NullPtzController:
    driver = "null"

    async def capability(self, source: dict[str, Any]) -> PtzCapability:
        return PtzCapability(move=False, zoom=False, presets=())

    async def move(self, source: dict[str, Any], *, pan: float, tilt: float, zoom: float,
                   mode: str = "continuous") -> None:
        raise NotImplementedError("PTZ is not available (PTZ_DRIVER=null)")

    async def stop(self, source: dict[str, Any]) -> None:
        return None

    async def presets(self, source: dict[str, Any]) -> list[dict[str, str]]:
        return []

    async def goto(self, source: dict[str, Any], token: str) -> None:
        raise NotImplementedError("PTZ is not available (PTZ_DRIVER=null)")


class NoneMotionDetector:
    driver = "none"

    async def poll(self, session_id: str, handle: EngineHandle) -> list[MotionEvent]:
        return []


class NoneDiscovery:
    driver = "none"

    async def discover(self, timeout_seconds: float = 3.0) -> list[DiscoveredCamera]:
        return []


class DirectMediaEgress:
    """No TURN — host/server-reflexive candidates only. Works on flat networks; reported, not faked."""
    driver = "direct"

    def ice_servers(self) -> list[dict[str, Any]]:
        return []


class TurnMediaEgress:
    driver = "turn"

    def __init__(self, urls: list[str], secret: str = "") -> None:
        self._urls = urls
        self._secret = secret

    def ice_servers(self) -> list[dict[str, Any]]:
        server: dict[str, Any] = {"urls": self._urls}
        if self._secret:
            # A shared-secret (coturn REST) credential; the username/expiry is minted per client at
            # offer time. Here we only advertise the server list — the secret never leaves the pod.
            server["credentialType"] = "password"
        return [server] if self._urls else []
