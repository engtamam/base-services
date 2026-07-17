"""OnvifPtzController — move a physical camera over ONVIF.

ONVIF (python-onvif-zeep, MIT) is an OPTIONAL, lazily-imported dependency — a deployment without PTZ
never pays for it. The `client_factory` seam lets the control logic be unit-tested with a fake camera
(no hardware), while the live path talks to a real ONVIF camera.

THE PTZ SAFETY RULE: a continuous move carries a Timeout, so a dropped "stop" cannot spin the motor
forever — the camera stops itself after PTZ_MOVE_TIMEOUT. That is the physical analogue of the
session guards: no command runs unbounded.
"""
from __future__ import annotations

from typing import Any, Callable

from app.ports.stream_types import PtzCapability


def _default_factory(source: dict[str, Any]):   # pragma: no cover - needs a camera + the lib
    from onvif import ONVIFCamera

    onvif = source.get("onvif") or {}
    cam = ONVIFCamera(onvif.get("host"), int(onvif.get("port", 80)),
                      source.get("user", ""), source.get("password", ""))
    return cam


class OnvifPtzController:
    driver = "onvif"

    def __init__(self, *, move_timeout_seconds: int = 5,
                 client_factory: Callable[[dict], Any] = _default_factory) -> None:
        self._timeout = move_timeout_seconds
        self._factory = client_factory

    def _ptz(self, source: dict[str, Any]):
        cam = self._factory(source)
        ptz = cam.create_ptz_service()
        media = cam.create_media_service()
        profile = media.GetProfiles()[0]
        return ptz, profile

    async def capability(self, source: dict[str, Any]) -> PtzCapability:
        try:
            ptz, profile = self._ptz(source)
            presets = ptz.GetPresets({"ProfileToken": profile.token}) or []
            return PtzCapability(move=True, zoom=True,
                                 presets=tuple({"token": str(getattr(p, "token", "")),
                                                "name": str(getattr(p, "Name", ""))} for p in presets))
        except Exception:   # noqa: BLE001 — a camera that cannot answer is simply not PTZ-capable
            return PtzCapability(move=False, zoom=False, presets=())

    async def move(self, source: dict[str, Any], *, pan: float, tilt: float, zoom: float,
                   mode: str = "continuous") -> None:
        ptz, profile = self._ptz(source)
        if mode == "continuous":
            req = {"ProfileToken": profile.token,
                   "Velocity": {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}},
                   # THE safety timeout: the camera stops itself even if our stop() never arrives.
                   "Timeout": f"PT{int(self._timeout)}S"}
            ptz.ContinuousMove(req)
        elif mode == "absolute":
            ptz.AbsoluteMove({"ProfileToken": profile.token,
                              "Position": {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}})
        else:
            ptz.RelativeMove({"ProfileToken": profile.token,
                              "Translation": {"PanTilt": {"x": pan, "y": tilt}, "Zoom": {"x": zoom}}})

    async def stop(self, source: dict[str, Any]) -> None:
        ptz, profile = self._ptz(source)
        ptz.Stop({"ProfileToken": profile.token, "PanTilt": True, "Zoom": True})

    async def presets(self, source: dict[str, Any]) -> list[dict[str, str]]:
        ptz, profile = self._ptz(source)
        presets = ptz.GetPresets({"ProfileToken": profile.token}) or []
        return [{"token": str(getattr(p, "token", "")), "name": str(getattr(p, "Name", ""))}
                for p in presets]

    async def goto(self, source: dict[str, Any], token: str) -> None:
        ptz, profile = self._ptz(source)
        ptz.GotoPreset({"ProfileToken": profile.token, "PresetToken": token})
