"""FfmpegMotionDetector — scene-change detection over the session's latest segment.

Motion is a scene change: ffmpeg's `select='gt(scene,TH)'` scores each frame's difference from the
previous, and `metadata=print` writes the scores that cross the threshold. We analyse the NEWEST
completed segment in the session's output dir (the same segments HLS/recording already produce — no
second decode of the live stream), remember which segments we've seen, and emit one MotionEvent per
segment that contained a scene change. Verifiable: a clip with a cut trips it; a static clip does not.
"""
from __future__ import annotations

import asyncio
import os

from app.core.models import now
from app.ports.stream_types import EngineHandle, MotionEvent


class FfmpegMotionDetector:
    driver = "ffmpeg-scene"

    def __init__(self, ffmpeg_path: str = "ffmpeg", threshold: float = 0.1) -> None:
        self._ffmpeg = ffmpeg_path or "ffmpeg"
        self._threshold = threshold
        self._seen: dict[str, str] = {}     # session_id -> last analysed segment name

    async def poll(self, session_id: str, handle: EngineHandle) -> list[MotionEvent]:
        out = handle.output_dir
        if not out:
            return []
        try:
            segs = sorted(n for n in os.listdir(out)
                          if (n.startswith("rec_") or n.startswith("index")) and
                          n.endswith((".mp4", ".ts")))
        except OSError:
            return []
        if not segs:
            return []
        newest = segs[-1]
        if self._seen.get(session_id) == newest:
            return []                        # already analysed this segment
        self._seen[session_id] = newest
        score = await self._scene_score(os.path.join(out, newest))
        if score >= self._threshold:
            return [MotionEvent(session_id=session_id, at=now(), score=score)]
        return []

    async def _scene_score(self, path: str) -> float:
        args = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "info",
                "-i", path, "-vf", f"select='gt(scene,{self._threshold})',metadata=print",
                "-an", "-f", "null", "-"]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await proc.communicate()
        best = 0.0
        for line in (err or b"").decode(errors="ignore").splitlines():
            if "scene_score" in line:
                try:
                    best = max(best, float(line.rsplit("=", 1)[1].strip()))
                except (ValueError, IndexError):
                    continue
        return best
