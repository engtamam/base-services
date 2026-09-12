"""Clip export: the feature the scaffolding described and nothing could perform.

`Export`, `ExportStore` and EXPORT_MAX_RANGE_SECONDS all existed. Nothing could produce a clip —
no exporter port, no adapter, no worker — so a route over that would have created rows stuck at
`pending` for ever.

The port names three things that must never happen, and this file is organised around them:

  1. a clip that SILENTLY covers less than it was asked for
  2. a clip reported ready that nobody can open
  3. an export that never finishes and never fails
"""
from __future__ import annotations

import asyncio

import pytest

from app.core.exporting import ExportService
from app.ports.clip_exporter import FAILED, READY, ClipResult


def run(c):
    return asyncio.run(c)


class _Exports:
    def __init__(self):
        self.rows: dict[str, object] = {}

    async def create(self, exp):
        self.rows[exp.id] = exp
        return exp

    async def get(self, export_id):
        return self.rows.get(export_id)

    async def save(self, exp):
        self.rows[exp.id] = exp
        return exp


class _Recordings:
    def __init__(self, recs):
        self.recs = recs

    async def list_for_tenant(self, tenant, source_id=None, limit=50, after=None):
        return [r for r in self.recs if r.tenant == tenant], None


class _Rec:
    def __init__(self, tenant, segments):
        self.tenant = tenant
        self.segments = segments


class _Sink:
    driver = "fake"

    def __init__(self):
        self.stored: list[bytes] = []

    async def signed_url(self, ref, *, ttl):
        return f"https://sink.example/{ref}?ttl={ttl}"

    async def put_segment(self, *, tenant, session_id, seq, data, content_type, start, end):
        self.stored.append(data)
        from app.ports.stream_types import SegmentRef
        return SegmentRef(ref=f"clip-{session_id}", bytes=len(data), start=start, end=end)


# The REAL memory adapter, not a stand-in. A hand-written fake implementing only `dequeue` is what
# let the pop-vs-lease bug survive here: every test passed while measuring a contract the shipped
# adapters do not have. Using the real driver means these tests break when the port changes.
from app.adapters.queue.memory import MemoryQueue as _Queue


class _Exporter:
    driver = "fake"

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    async def export(self, *, sources, fmt, start, end):
        self.calls.append({"sources": list(sources), "fmt": fmt})
        return self.result


_CFG = {"max_range_seconds": 3600, "formats": ("mp4",), "max_segments": 5000,
        "max_recordings": 1000, "segment_url_ttl": 3600,
        "lease_seconds": 900}


def _svc(exporter=None, recs=(), cfg=None):
    return ExportService(
        exports=_Exports(), recordings=_Recordings(list(recs)),
        exporter=exporter or _Exporter(ClipResult(status=READY, data=b"CLIP",
                                                  content_type="video/mp4")),
        sink=_Sink(), queue=_Queue(), cfg={**_CFG, **(cfg or {})})


def _seg(start, end, ref="s1"):
    return {"ref": ref, "bytes": 10, "start": start, "end": end}


T0 = "2026-07-19T10:00:00+00:00"
T30 = "2026-07-19T10:30:00+00:00"
T60 = "2026-07-19T11:00:00+00:00"


# ── request: bounded work ──────────────────────────────────────────────────────
def test_a_request_is_recorded_pending_and_enqueued():
    svc = _svc()
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    assert exp.status == "pending"
    # Read through the port rather than a fake's inbox: the assertion is that the job is
    # RETRIEVABLE, which is the property the caller depends on.
    job = run(svc._queue.dequeue("stream.exports", lease_seconds=60))
    assert job is not None and job.payload == {"export_id": exp.id, "tenant": "acme"}


def test_the_row_exists_BEFORE_the_job_is_enqueued():
    """The other order loses the export entirely if the process dies between the two: a job
    would reference a row that was never written."""
    svc = _svc()
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    assert exp.id in svc._exports.rows


def test_a_range_longer_than_the_limit_is_REFUSED():
    """EXPORT_MAX_RANGE_SECONDS existed and was never read — a caller could ask for a year and
    the worker would try. Bounded work is the difference between a slow export and an outage."""
    svc = _svc(cfg={"max_range_seconds": 60})
    with pytest.raises(ValueError) as e:
        run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    assert "EXPORT_MAX_RANGE_SECONDS" in str(e.value)


def test_an_inverted_range_is_refused():
    svc = _svc()
    with pytest.raises(ValueError):
        run(svc.request(tenant="acme", source_id="cam1", start=T60, end=T0, fmt="mp4"))


def test_an_unconfigured_format_is_refused_at_REQUEST_time():
    """Not on the worker. A caller learns immediately instead of polling a job that was never
    going to produce anything."""
    svc = _svc()
    with pytest.raises(ValueError):
        run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mkv"))


# ── 1. a clip that silently covers less than it was asked for ──────────────────
def test_a_gap_in_the_footage_is_REPORTED():
    """THE one that matters most. A reviewer not told about a camera outage concludes nothing
    happened during it."""
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30)])])          # second half missing
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    out = run(svc.run_one())
    assert out["missing_seconds"] == pytest.approx(1800.0)


def test_full_coverage_reports_no_gap():
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30), _seg(T30, T60, "s2")])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    assert run(svc.run_one())["missing_seconds"] == 0.0


def test_OVERLAPPING_segments_do_not_hide_a_gap():
    """Summing durations would UNDER-report the gap whenever two segments overlap — which is
    exactly when a camera restarted, i.e. when a gap is most likely."""
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30), _seg(T0, T30, "dup")])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    assert run(svc.run_one())["missing_seconds"] == pytest.approx(1800.0)


def test_segments_are_passed_in_TIME_ORDER():
    """Concatenating out of order produces a clip that jumps backwards — plausible-looking and
    useless as a record of what happened."""
    svc = _svc(recs=[_Rec("acme", [_seg(T30, T60, "second"), _seg(T0, T30, "first")])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T60, fmt="mp4"))
    run(svc.run_one())
    urls = [s.url for s in svc._exporter.calls[0]["sources"]]
    assert "first" in urls[0] and "second" in urls[1]


def test_segments_outside_the_window_are_not_included():
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30), _seg("2026-07-20T00:00:00+00:00",
                                                       "2026-07-20T01:00:00+00:00", "later")])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    run(svc.run_one())
    assert len(svc._exporter.calls[0]["sources"]) == 1


# ── 2. a clip reported ready that nobody can open ──────────────────────────────
def test_an_exporter_failure_marks_the_export_FAILED_not_ready():
    svc = _svc(exporter=_Exporter(ClipResult(status=FAILED, detail="ffmpeg exited 1")),
               recs=[_Rec("acme", [_seg(T0, T30)])])
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    out = run(svc.run_one())
    assert out["status"] == FAILED
    assert svc._exports.rows[exp.id].status == "failed"
    assert svc._exports.rows[exp.id].file_id == "", "a failed export must not carry a file"


def test_an_UNRECOGNISED_exporter_status_is_not_treated_as_success():
    """`ok` is `== READY`, never `!= FAILED`."""
    svc = _svc(exporter=_Exporter(ClipResult(status="probably_fine")),
               recs=[_Rec("acme", [_seg(T0, T30)])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    assert run(svc.run_one())["status"] == FAILED


def test_a_successful_export_stores_the_clip_and_records_the_ref():
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30)])])
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    out = run(svc.run_one())
    assert out["status"] == READY
    row = svc._exports.rows[exp.id]
    assert row.status == "ready" and row.file_id
    assert svc._sink.stored == [b"CLIP"]


def test_a_window_with_NO_footage_fails_rather_than_producing_an_empty_clip():
    """The null-ish case the exporter also refuses: an empty file reported ready is the silent
    hole failure at its worst — a clip that proves nothing happened."""
    from app.adapters.clip_exporter.null import NullClipExporter

    svc = _svc(exporter=NullClipExporter(), recs=[])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    assert run(svc.run_one())["status"] == FAILED


# ── the worker ─────────────────────────────────────────────────────────────────
def test_an_empty_queue_returns_None_so_a_drain_can_stop():
    assert run(_svc().run_one()) is None


def test_an_export_already_handled_is_SKIPPED_not_redone():
    """Two replicas draining the same queue must not re-export the same clip."""
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30)])])
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    svc._exports.rows[exp.id].status = "ready"
    assert run(svc.run_one())["skipped"] is True


def test_an_export_erased_between_enqueue_and_dequeue_is_not_an_error():
    """A DSAR deletes exports. The job outliving the row is expected, and reported as `gone` so a
    queue quietly dropping work still looks different from an idle one."""
    svc = _svc()
    exp = run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    del svc._exports.rows[exp.id]
    assert run(svc.run_one())["status"] == "gone"


def test_drain_is_bounded_by_its_cap():
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30)])])
    for _ in range(5):
        run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    assert run(svc.drain(2))["processed"] == 2


def test_drain_stops_when_the_queue_empties():
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T30)])])
    run(svc.request(tenant="acme", source_id="cam1", start=T0, end=T30, fmt="mp4"))
    assert run(svc.drain(10))["processed"] == 1


# ── the null adapter ───────────────────────────────────────────────────────────
def test_the_null_exporter_FAILS_rather_than_faking_success_or_hanging():
    """Both alternatives leave a customer waiting for a download that never arrives."""
    from app.adapters.clip_exporter.null import NullClipExporter

    r = run(NullClipExporter().export(sources=[], fmt="mp4", start=T0, end=T30))
    assert r.status == FAILED and r.ok is False
    assert "EXPORT_DRIVER" in r.detail


# ── the boot guard ─────────────────────────────────────────────────────────────
def _settings(**kw):
    from app.config import Settings
    base = dict(INTERNAL_SERVICE_TOKEN="x" * 40)
    base.update(kw)
    return Settings(**base)


def test_production_refuses_to_offer_exports_it_cannot_produce():
    s = _settings(ENV="production", EXPORT_ENABLED=True, EXPORT_DRIVER="null")
    with pytest.raises(ValueError) as e:
        s.assert_export_config_ok()
    assert "EXPORT_DRIVER" in str(e.value)


def test_a_platform_that_does_not_offer_exports_is_unaffected():
    """EXPORT_ENABLED separates 'we do not do clip export' from 'somebody forgot to configure the
    exporter'. Those look identical from the driver alone — which is how a model, a store and a
    config knob sat here with nothing able to produce a clip."""
    _settings(ENV="production", EXPORT_ENABLED=False, EXPORT_DRIVER="null").assert_export_config_ok()


def test_a_format_with_no_ARGS_entry_fails_at_boot_not_on_the_worker():
    """Otherwise the export is accepted and dies later, which is the pending-for-ever shape."""
    s = _settings(EXPORT_FORMATS="mp4,mkv", EXPORT_ARGS='{"mp4": ["-c", "copy"]}')
    with pytest.raises(ValueError) as e:
        s.assert_export_config_ok()
    assert "mkv" in str(e.value)


def test_malformed_export_args_fail_at_boot():
    with pytest.raises(ValueError):
        _settings(EXPORT_ARGS="{not json").assert_export_config_ok()


def test_non_iso_timestamps_are_rejected_not_bypassing_the_range_cap():
    # audit stream-07: non-ISO strings where end sorts after start lexically (e.g. 'a' < 'z') used to
    # pass the string 'end<=start' check and yield span 0.0, bypassing max_range_seconds. Now rejected.
    import pytest
    svc = _svc(recs=[_Rec("acme", [_seg(T0, T60)])])
    with pytest.raises(ValueError):
        run(svc.request(tenant="acme", source_id="cam1", start="a", end="z", fmt="mp4"))
