"""NullStreamEngine — a synthetic engine so the whole service boots and tests with ZERO infra.

It does not open a real source; it fabricates plausible stats, a tiny HLS playlist, and a 1x1 JPEG
snapshot. Every state transition the core drives (start → alive → stats → stop) behaves exactly as a
real engine would, so the session lifecycle, guards, placement, and outputs are all exercised without
ffmpeg or a camera. The real ffmpeg engine (Phase 35.3) is a drop-in for this.
"""
from __future__ import annotations

import asyncio
import os

from app.ports.stream_types import EngineHandle, EngineSpec, EngineStats, ProbeResult

# Smallest valid JPEG (1x1) so a snapshot returns real image bytes.
_ONE_PX_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300080606070605080707"
    "07090908"
    "0a0c140d0c0b0b0c1912130f141d1a1f1e1d1a1c1c20242e2720222c231c1c283729"
    "2c30313434341f27393d38323c2e333432ffc0000b080001000101011100ffc40014"
    "00010000000000000000000000000000000009ffc40014100100000000000000000000"
    "00000000000000ffda0008010100003f00d2cf20ffd9")


class NullStreamEngine:
    driver = "null"

    def __init__(self) -> None:
        self._running: dict[str, dict] = {}
        self._seq = 0

    async def start(self, spec: EngineSpec) -> EngineHandle:
        self._seq += 1
        hid = f"null-{self._seq}"

        def _seed_dir() -> None:   # blocking disk I/O off the loop (uniform with the real engine)
            os.makedirs(spec.output_dir, exist_ok=True)
            if "hls" in spec.outputs:
                # A minimal live playlist so the HLS output has something to point at.
                with open(os.path.join(spec.output_dir, "index.m3u8"), "w", encoding="utf-8") as fh:
                    fh.write("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n")

        await asyncio.to_thread(_seed_dir)
        self._running[hid] = {"bytes": 0, "frames": 0}
        return EngineHandle(id=hid, pid=None, output_dir=spec.output_dir)

    async def stop(self, handle: EngineHandle) -> None:
        self._running.pop(handle.id, None)

    async def stats(self, handle: EngineHandle) -> EngineStats:
        st = self._running.get(handle.id)
        if st is None:
            return EngineStats(alive=False)
        st["bytes"] += 250_000     # ~2 Mbit/s of synthetic throughput per poll
        st["frames"] += 25
        return EngineStats(fps=25.0, bitrate=2_000_000, bytes_out=st["bytes"],
                           frames=st["frames"], last_frame_at="synthetic", alive=True)

    async def is_alive(self, handle: EngineHandle) -> bool:
        return handle.id in self._running

    async def snapshot(self, source_url: str, *, transport: str = "tcp",
                       max_width: int = 1280) -> bytes:
        return _ONE_PX_JPEG

    async def probe(self, source_url: str, *, transport: str = "tcp") -> ProbeResult:
        return ProbeResult(codec="h264", width=1920, height=1080, fps=25.0,
                           has_audio=True, ptz_capable=False, ok=True)
