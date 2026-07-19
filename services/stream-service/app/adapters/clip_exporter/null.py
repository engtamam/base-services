"""Null exporter — produces nothing, and says so on every call.

The default, because a service must boot with zero infrastructure and ffmpeg is not always there.

It returns FAILED, not a pending state and certainly not a fake success. That choice is the whole
reason this adapter exists rather than the feature simply being absent: an export that reports
`ready` with no file, or one that sits at `pending` for ever, both leave a customer waiting for a
download that will never arrive. A refusal with a reason is the only honest answer a service can
give when it has no exporter configured, and the production boot guard refuses to start on it when
exports are enabled.
"""
from __future__ import annotations

from app.ports.clip_exporter import FAILED, ClipResult, ClipSource


class NullClipExporter:
    driver = "null"

    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    async def export(self, *, sources: list[ClipSource], fmt: str,
                     start: str, end: str) -> ClipResult:
        # Recorded so a test can prove the exporter was REACHED rather than skipped.
        self.calls.append((len(sources), fmt))
        return ClipResult(
            status=FAILED,
            detail="null clip exporter: no export was produced (by design). Set "
                   "EXPORT_DRIVER=ffmpeg and EXPORT_FFMPEG_BIN to enable clip export.",
        )
