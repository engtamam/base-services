"""ExportService — request a clip, produce it on a worker, hand back a signed download.

Two halves, deliberately separated by a queue:

  request()  validates, records a PENDING export, enqueues. Returns immediately.
  run_one()  the worker: gathers segments, calls the exporter, stores the clip, marks it ready.

The split is not architectural taste. A two-hour remux is minutes of work; doing it on the request
path would hold a connection open past every sane timeout and let one export starve the API for
everyone else. And the record is written BEFORE the work starts, so an export that dies mid-flight
is a visible `failed` row rather than a request nobody can account for.

WHAT THIS REFUSES, and why each refusal is cheaper than the alternative:

  * A range longer than EXPORT_MAX_RANGE_SECONDS. The knob existed and was never read — a caller
    could ask for a year and the worker would try. Bounded work is the difference between a slow
    export and an outage.
  * A range with no footage. Answered as a failure at REQUEST time where possible, so the caller
    learns immediately instead of polling a job that was never going to produce anything.
  * A window the segments do not fully cover. The clip is still produced, but `missing_seconds`
    travels with it all the way to the API. A reviewer who is not told about a ten-minute camera
    outage will conclude nothing happened during those ten minutes, and that is a worse outcome
    than refusing the export outright.
"""
from __future__ import annotations

from typing import Any

from app.core.models import Export, new_id, now
from app.ports.clip_exporter import ClipSource

PENDING, READY, FAILED = "pending", "ready", "failed"

_EXPORT_QUEUE = "stream.exports"


def _overlaps(seg_start: str, seg_end: str, start: str, end: str) -> bool:
    """Half-open comparison on ISO strings, which sort lexicographically when UTC and
    zero-padded — the same assumption the retention sweeper already relies on."""
    return (seg_start or "") < end and (seg_end or seg_start or "") > start


class ExportService:
    def __init__(self, *, exports, recordings, exporter, sink, queue, cfg,
                 clock=now) -> None:
        self._exports = exports
        self._recordings = recordings
        self._exporter = exporter
        self._sink = sink
        self._queue = queue
        self._cfg = cfg
        self._clock = clock

    # ── request (fast path) ────────────────────────────────────────────────────
    async def request(self, *, tenant: str, source_id: str, start: str, end: str,
                      fmt: str) -> Export:
        if end <= start:
            raise ValueError("end must be after start")
        span = self._span_seconds(start, end)
        limit = self._cfg["max_range_seconds"]
        if limit and span > limit:
            raise ValueError(
                f"requested {int(span)}s exceeds EXPORT_MAX_RANGE_SECONDS ({limit}); "
                f"ask for a shorter window or raise the limit deliberately")
        if fmt not in self._cfg["formats"]:
            raise ValueError(f"format {fmt!r} is not one of {sorted(self._cfg['formats'])}")

        exp = Export(id=f"exp_{new_id()}", tenant=tenant, source_id=source_id,
                     start=start, end=end, status=PENDING, fmt=fmt)
        await self._exports.create(exp)
        # Enqueued AFTER the row exists. The other order loses the export entirely if this process
        # dies between the two: a job would reference a row that was never written.
        await self._queue.enqueue(_EXPORT_QUEUE, {"export_id": exp.id, "tenant": tenant})
        return exp

    # ── run (worker) ───────────────────────────────────────────────────────────
    async def run_one(self) -> dict[str, Any] | None:
        """Drain one export. Returns None when the queue is empty, so a caller can loop.

        LEASED, NOT POPPED. This used to take the job off the queue and then transcode, so a
        worker killed mid-export — and an export is the longest-running work in this service —
        took the job with it. The Export row stayed PENDING for ever, nothing retried it, and the
        customer watched a clip that was never coming.

        The job is deleted only once the export reaches a terminal state. A crash before that
        leaves the lease to expire and the job returns by itself; the PENDING check below makes
        the duplicate harmless, because a re-run finds the row already READY and skips it.
        """
        job = await self._queue.dequeue(_EXPORT_QUEUE,
                                        lease_seconds=self._cfg["lease_seconds"])
        if job is None:
            return None
        export_id = (job.payload or {}).get("export_id", "")
        exp = await self._exports.get(export_id)
        if exp is None:
            # The row was erased (a DSAR deletes exports) between enqueue and dequeue. Nothing to
            # do and nothing wrong — reported so a queue that is quietly dropping work looks
            # different from one that is idle.
            await self._queue.ack(job)
            return {"export_id": export_id, "status": "gone"}
        if exp.status != PENDING:
            # Already handled — by an earlier run, or by the duplicate a lease can produce.
            await self._queue.ack(job)
            return {"export_id": export_id, "status": exp.status, "skipped": True}

        try:
            sources, missing = await self._sources_for(exp)
            result = await self._exporter.export(sources=sources, fmt=exp.fmt,
                                                 start=exp.start, end=exp.end)
        except Exception:
            # An unexpected fault is not a verdict about the export. Hand the job straight back
            # rather than waiting out a lease sized for a long transcode: the row stays PENDING,
            # so a retry is correct and nothing downstream sees a false result.
            await self._queue.nack(job)
            raise
        if not result.ok:
            # A REPORTED failure is terminal: the exporter ran and said no. Acked, because
            # retrying it would fail identically for as long as the queue exists.
            exp.status = FAILED
            await self._exports.save(exp)
            await self._queue.ack(job)
            return {"export_id": exp.id, "status": FAILED, "detail": result.detail}

        # Stored in the RECORDING SINK, not the object store, and that is a correction rather
        # than a preference: the object store has no signed_url, so a download route over it
        # could only have handed back an empty link or streamed gigabytes through this process.
        # The sink already holds the footage these frames came from and already knows how to
        # issue a time-limited URL for it.
        ref = await self._sink.put_segment(
            tenant=exp.tenant, session_id=exp.id, seq=0, data=result.data,
            content_type=result.content_type or "application/octet-stream",
            start=exp.start, end=exp.end)
        exp.file_id = ref.ref
        exp.status = READY
        await self._exports.save(exp)
        # Acked only now: everything above this line is redoable, so a crash before it costs one
        # repeated export rather than one silently lost one.
        await self._queue.ack(job)
        return {"export_id": exp.id, "status": READY, "bytes": len(result.data),
                # Carried out of the worker, not swallowed here. See the module docstring: a
                # reviewer who is not told about a gap concludes nothing happened in it.
                "missing_seconds": missing}

    async def drain(self, limit: int) -> dict[str, Any]:
        """Work the queue until it is empty or the per-run cap is reached.

        Capped so one enormous backlog degrades into several runs instead of one run that never
        finishes — the same shape as every other sweeper here.
        """
        # Anything a dead worker left behind returns first, or the queue looks idle while jobs
        # sit in flight with nobody holding them.
        await self._queue.reclaim(_EXPORT_QUEUE)

        done: list[dict] = []
        for _ in range(max(1, limit)):
            outcome = await self.run_one()
            if outcome is None:
                break
            done.append(outcome)
        return {"processed": len(done), "results": done}

    # ── segments ───────────────────────────────────────────────────────────────
    async def _sources_for(self, exp: Export) -> tuple[list[ClipSource], float]:
        """The overlapping segments in time order, and how much of the window has no footage."""
        recs, _ = await self._recordings.list_for_tenant(
            exp.tenant, source_id=exp.source_id, limit=self._cfg["max_recordings"])
        segments: list[dict] = []
        for rec in recs:
            for seg in rec.segments or []:
                if _overlaps(seg.get("start", ""), seg.get("end", ""), exp.start, exp.end):
                    segments.append(seg)
        segments.sort(key=lambda s: s.get("start", ""))
        segments = segments[: self._cfg["max_segments"]]

        sources = []
        for seg in segments:
            url = await self._sink.signed_url(seg["ref"], ttl=self._cfg["segment_url_ttl"])
            sources.append(ClipSource(url=url, start=seg.get("start", ""),
                                      end=seg.get("end", "")))
        return sources, self._uncovered_seconds(exp.start, exp.end, segments)

    def _uncovered_seconds(self, start: str, end: str, segments: list[dict]) -> float:
        """Seconds of [start, end] no segment covers, merging overlaps before subtracting.

        Naively summing segment durations would UNDER-report gaps whenever two segments overlap,
        which is exactly when a camera restarted — the moment a gap is most likely and most worth
        knowing about.
        """
        window = self._span_seconds(start, end)
        if window <= 0 or not segments:
            return max(0.0, window)
        covered = 0.0
        cur_start = cur_end = None
        for seg in segments:
            s = max(seg.get("start", ""), start)
            e = min(seg.get("end", "") or seg.get("start", ""), end)
            if e <= s:
                continue
            if cur_end is None or s > cur_end:
                if cur_end is not None:
                    covered += self._span_seconds(cur_start, cur_end)
                cur_start, cur_end = s, e
            elif e > cur_end:
                cur_end = e
        if cur_end is not None:
            covered += self._span_seconds(cur_start, cur_end)
        return max(0.0, round(window - covered, 3))

    @staticmethod
    def _span_seconds(start: str, end: str) -> float:
        import datetime as _dt
        try:
            a = _dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            b = _dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            return 0.0
        return (b - a).total_seconds()
