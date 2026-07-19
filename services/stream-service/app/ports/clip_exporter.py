"""ClipExporter port — turn a time range of recorded segments into one downloadable clip.

This service recorded continuously and could not hand anybody a piece of it. `Export`, `ExportStore`
and EXPORT_MAX_RANGE_SECONDS all existed; nothing could produce a clip, so the model described a
feature that was never built. A route over that scaffolding would have created rows stuck at
`pending` for ever — a visible feature that never completes, which is worse than an honest gap.

WHAT AN EXPORT ACTUALLY IS. Recording writes fixed-length segments (RECORD_SEGMENT_SECONDS) into
the RecordingSink. A clip for [start, end] is the segments that OVERLAP that window, concatenated
and remuxed into one container. Nothing is re-encoded in the default path: a remux is a copy of
the same bitstream into a new container, so it is fast, lossless, and cannot be blamed for
degrading evidence. Re-encoding is available by configuration for platforms that need a smaller
file, and it is opt-in precisely because it changes the pixels.

WHY THE PORT TAKES URLS, NOT BYTES. Segments live wherever the sink put them — file-service, S3,
a local directory — and the sink already knows how to produce a time-limited signed URL for one.
Handing the exporter URLs means every sink works without teaching the port to read from each of
them, and it keeps whole recordings out of this service's memory: a two-hour export is gigabytes,
and buffering it to build a clip is how a stream service falls over at the worst moment.

WHAT MUST NEVER HAPPEN, in order:

  1. A clip that SILENTLY covers less than it was asked for. A gap in the segments — a camera
     offline for ten minutes — must be reported, never quietly concatenated away. Someone
     reviewing an incident would otherwise conclude nothing happened during those minutes.
  2. A clip reported ready that nobody can open. A zero-byte or truncated output is a FAILURE,
     however cleanly the process exited.
  3. An export that never finishes and never fails. Every path here is bounded by a timeout.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

READY = "ready"
FAILED = "failed"


@dataclass(frozen=True)
class ClipSource:
    """One recorded segment, as the exporter will read it."""

    url: str
    start: str
    end: str


@dataclass(frozen=True)
class ClipResult:
    """The outcome. `status` is READY or FAILED — there is no third, hopeful state."""

    status: str
    data: bytes = b""
    content_type: str = ""
    detail: str = ""
    # Seconds of the requested window NOT covered by any segment. Zero means complete; anything
    # else is carried all the way to the caller, because an export with silent holes in it is
    # evidence of the wrong thing.
    missing_seconds: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Written `== READY`, never `!= FAILED`, so a status this port has not seen yet cannot
        become a successful export by default."""
        return self.status == READY


@runtime_checkable
class ClipExporter(Protocol):
    driver: str

    async def export(self, *, sources: list[ClipSource], fmt: str,
                     start: str, end: str) -> ClipResult:
        """Never raises. A failure is a ClipResult(FAILED, detail=...), because an exporter that
        threw would take down the worker draining the queue and every other export with it."""
        ...
