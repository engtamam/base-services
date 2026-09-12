"""RecordingShipper — move completed recording segments off the node into durable storage.

The engine writes `rec_00000000.<ext>` segments to the session's local output dir. Those are on ONE
pod's disk — a node loss loses them, and they never appear in the recordings API. The shipper moves
each COMPLETED segment to the recording sink (file-service / s3), appends it to the session's
Recording, and deletes the local copy so the disk does not fill.

"Completed" matters: ffmpeg is still writing the highest-numbered segment, so we never ship the last
one — shipping a half-written segment would store a truncated file. Idempotent: a shipped segment is
deleted locally, so a re-run never double-ships. Driven by the sweep CronJob.
"""
from __future__ import annotations

import asyncio
import os
import time

from app.core.models import Recording, new_id, now
from app.core.stores import RecordingStore


class RecordingShipper:
    def __init__(self, *, sink, recordings: RecordingStore, ship_lease_seconds: float = 300.0) -> None:
        self._sink = sink
        self._recordings = recordings
        self._lease_ttl = ship_lease_seconds

    async def ship_session(self, *, tenant: str, session_id: str, source_id: str,
                           output_dir: str, record_ext: str = "mp4",
                           final: bool = False) -> int:
        """Ship every completed rec_ segment in `output_dir`. Returns how many were shipped.

        `final=True` (the session is stopping) ships the last segment too, since ffmpeg has closed it.
        """
        try:
            names = sorted(n for n in os.listdir(output_dir)
                           if n.startswith("rec_") and n.endswith(f".{record_ext}"))
        except OSError:
            return 0
        if not names:
            return 0
        # Hold back the newest segment while the stream is live (still being written).
        ship = names if final else names[:-1]
        if not ship:
            return 0

        # One shipper per session at a time: two concurrent sweeps (or a sweep racing a final stop)
        # both listed the same files and both shipped + appended, double-storing segments and losing
        # rec.segments appends to a last-writer-wins save. The lease serializes them.
        if not await self._recordings.acquire_ship_lease(session_id, time.time(), self._lease_ttl):
            return 0
        try:
            rec = await self._get_or_create(tenant, session_id, source_id)
            shipped = 0
            for name in ship:
                path = os.path.join(output_dir, name)

                def _read(p: str = path) -> bytes:   # blocking disk read off the loop (large segments)
                    with open(p, "rb") as fh:
                        return fh.read()

                try:
                    data = await asyncio.to_thread(_read)
                except OSError:
                    continue
                seq = _seq_of(name)
                ref = await self._sink.put_segment(
                    tenant=tenant, session_id=session_id, seq=seq, data=data,
                    content_type="video/mp4", start=now(), end=now())
                rec.segments.append({"ref": ref.ref, "bytes": ref.bytes, "start": ref.start,
                                     "end": ref.end, "seq": seq})
                rec.total_bytes += ref.bytes
                try:
                    os.remove(path)     # shipped → free the local copy (idempotent on re-run)
                except OSError:
                    pass
                shipped += 1
            if final:
                rec.ended = now()
            await self._recordings.save(rec)
            return shipped
        finally:
            await self._recordings.release_ship_lease(session_id)

    async def _get_or_create(self, tenant: str, session_id: str, source_id: str) -> Recording:
        # One Recording per session, keyed by session id for idempotent lookup.
        existing = await self._recordings.get(session_id)
        if existing is not None:
            return existing
        rec = Recording(id=session_id, tenant=tenant, source_id=source_id,
                        session_id=session_id, started=now(), trigger="continuous")
        return await self._recordings.create(rec)


def _seq_of(name: str) -> int:
    digits = "".join(c for c in name if c.isdigit())
    return int(digits) if digits else 0
