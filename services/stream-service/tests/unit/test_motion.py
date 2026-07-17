"""Motion → triggers → events, with debounce (fake detector, deterministic clock)."""
from __future__ import annotations

import asyncio

from app.core.models import LIVE, Session, new_id
from app.core.stores import TriggerStore
from app.ports.stream_types import MotionEvent
from tests.unit.test_stream_core import _Clock, _svc, _register, run


class _Detector:
    """Reports motion on demand — flip .motion to control it."""
    driver = "fake"

    def __init__(self): self.motion = True
    async def poll(self, session_id, handle):
        return [MotionEvent(session_id=session_id, at="now", score=0.9)] if self.motion else []


class _Bus:
    def __init__(self): self.events = []
    async def publish(self, topic, payload): self.events.append((topic, payload))


def _live_recording_session(svc, ds, source_id):
    sess = Session(id=new_id(), tenant="t1", source_id=source_id, status=LIVE, outputs=["hls"],
                   placement={"handle_id": "h1", "output_dir": "/tmp/x"})
    run(svc._sessions.create(sess))
    return sess


def test_motion_fires_trigger_event_with_actions():
    clock = _Clock()
    bus = _Bus()
    svc, ds = _svc(clock=clock)
    svc._events = bus
    src = _register(svc)
    sess = _live_recording_session(svc, ds, src.id)
    trig = TriggerStore(ds)
    run(trig.put(src.id, "t1", [{"on": "motion", "do": ["record", "notify"]}]))

    res = run(svc.poll_motion(_Detector(), trig, debounce_s=30))
    assert res["motion"] == 1
    topic, payload = bus.events[-1]
    assert topic == "stream.motion.detected"
    assert payload["source_id"] == src.id and payload["actions"] == ["notify", "record"]


def test_motion_is_debounced():
    clock = _Clock()
    bus = _Bus()
    svc, ds = _svc(clock=clock)
    svc._events = bus
    src = _register(svc)
    _live_recording_session(svc, ds, src.id)
    trig = TriggerStore(ds)
    run(trig.put(src.id, "t1", [{"on": "motion", "do": ["notify"]}]))
    det = _Detector()

    assert run(svc.poll_motion(det, trig, debounce_s=60))["motion"] == 1
    clock.advance(30)                                  # within debounce window
    assert run(svc.poll_motion(det, trig, debounce_s=60))["motion"] == 0
    clock.advance(31)                                  # past the window (30+31 > 60)
    assert run(svc.poll_motion(det, trig, debounce_s=60))["motion"] == 1


def test_no_motion_no_event():
    bus = _Bus()
    svc, ds = _svc()
    svc._events = bus
    src = _register(svc)
    _live_recording_session(svc, ds, src.id)
    det = _Detector(); det.motion = False
    assert run(svc.poll_motion(det, TriggerStore(ds), debounce_s=30))["motion"] == 0
    assert bus.events == []
