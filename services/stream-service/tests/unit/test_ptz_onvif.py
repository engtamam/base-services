"""PTZ (ONVIF) control logic + the move-timeout safety rule, and discovery shaping — with fakes."""
from __future__ import annotations

import asyncio

from app.adapters.stream.onvif_discovery import OnvifDiscovery
from app.adapters.stream.onvif_ptz import OnvifPtzController


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakePtz:
    def __init__(self): self.calls = []
    def ContinuousMove(self, req): self.calls.append(("continuous", req))
    def AbsoluteMove(self, req): self.calls.append(("absolute", req))
    def RelativeMove(self, req): self.calls.append(("relative", req))
    def Stop(self, req): self.calls.append(("stop", req))
    def GetPresets(self, req): return [_P("p1", "Door"), _P("p2", "Gate")]
    def GotoPreset(self, req): self.calls.append(("goto", req))


class _P:
    def __init__(self, token, name): self.token = token; self.Name = name


class _Profile:
    token = "prof0"


class _FakeCam:
    def __init__(self, ptz): self._ptz = ptz
    def create_ptz_service(self): return self._ptz
    def create_media_service(self):
        class _M:
            def GetProfiles(self): return [_Profile()]
        return _M()


def _controller(ptz, timeout=5):
    return OnvifPtzController(move_timeout_seconds=timeout,
                             client_factory=lambda source: _FakeCam(ptz))


def test_continuous_move_carries_the_safety_timeout():
    ptz = _FakePtz()
    ctl = _controller(ptz, timeout=7)
    run(ctl.move({}, pan=0.5, tilt=-0.2, zoom=0.0, mode="continuous"))
    kind, req = ptz.calls[0]
    assert kind == "continuous"
    assert req["Timeout"] == "PT7S"                 # the camera stops itself if our stop() is lost
    assert req["Velocity"]["PanTilt"] == {"x": 0.5, "y": -0.2}


def test_absolute_and_relative_moves():
    ptz = _FakePtz()
    ctl = _controller(ptz)
    run(ctl.move({}, pan=0.1, tilt=0.1, zoom=0.0, mode="absolute"))
    run(ctl.move({}, pan=0.1, tilt=0.1, zoom=0.0, mode="relative"))
    assert [c[0] for c in ptz.calls] == ["absolute", "relative"]


def test_stop_and_presets_and_goto():
    ptz = _FakePtz()
    ctl = _controller(ptz)
    run(ctl.stop({}))
    presets = run(ctl.presets({}))
    assert {p["name"] for p in presets} == {"Door", "Gate"}
    run(ctl.goto({}, "p2"))
    assert ("goto", {"ProfileToken": "prof0", "PresetToken": "p2"}) in ptz.calls


def test_capability_reports_true_when_camera_answers():
    cap = run(_controller(_FakePtz()).capability({}))
    assert cap.move and cap.zoom and len(cap.presets) == 2


def test_capability_false_when_camera_unreachable():
    def _boom(source): raise RuntimeError("no camera")
    ctl = OnvifPtzController(client_factory=_boom)
    cap = run(ctl.capability({}))
    assert cap.move is False and cap.presets == ()


def test_discovery_shapes_and_dedups_xaddrs():
    def _probe(timeout):
        return ["http://192.168.1.10/onvif/device", "http://192.168.1.10/onvif/dup",
                "http://192.168.1.20/onvif/device"]
    cams = run(OnvifDiscovery(probe=_probe).discover())
    hosts = {c.onvif_host for c in cams}
    assert hosts == {"192.168.1.10", "192.168.1.20"}   # deduped by host
