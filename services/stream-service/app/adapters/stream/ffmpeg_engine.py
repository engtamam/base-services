"""FfmpegStreamEngine — the real engine: ffmpeg opens the source and produces the outputs.

A drop-in for NullStreamEngine. ffmpeg is invoked as an EXTERNAL tool (like clamd/postgres) — never
shipped, never linked — so the license posture is unchanged; the operator provides the binary via
`FFMPEG_PATH`. The bytes flow ffmpeg → disk/bucket → viewer; this process only spawns and supervises.

  start()    spawn a long-lived ffmpeg that writes HLS segments (and, if `record`, MP4 segments)
  stop()     terminate the process (graceful, then kill), so no engine outlives its session
  stats()    liveness from the process + bytes from the output dir (drives the size guard)
  snapshot() one-shot ffmpeg grabbing a single JPEG frame
  probe()    ffprobe → codec/resolution/fps/audio
"""
from __future__ import annotations

import asyncio
import json
import os
import time

from app.ports.stream_types import EngineHandle, EngineSpec, EngineStats, ProbeResult


class FfmpegStreamEngine:
    driver = "ffmpeg"

    def __init__(self, ffmpeg_path: str = "ffmpeg", ffprobe_path: str = "") -> None:
        self._ffmpeg = ffmpeg_path or "ffmpeg"
        # Derive ffprobe next to ffmpeg unless given explicitly.
        self._ffprobe = ffprobe_path or _sibling(self._ffmpeg, "ffprobe")
        self._procs: dict[str, dict] = {}
        self._seq = 0

    # ── input helpers ──────────────────────────────────────────────────────────
    def _input_args(self, spec: EngineSpec) -> list[str]:
        args: list[str] = []
        if spec.source_url.startswith("rtsp"):
            args += ["-rtsp_transport", "udp" if spec.transport == "udp" else "tcp"]
        args += ["-i", spec.source_url]
        return args

    async def start(self, spec: EngineSpec) -> EngineHandle:
        os.makedirs(spec.output_dir, exist_ok=True)
        args = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error",
                *self._input_args(spec)]

        wrote_output = False
        if "hls" in spec.outputs:
            playlist = os.path.join(spec.output_dir, "index.m3u8")
            args += ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "hls",
                     "-hls_time", str(spec.hls_segment_seconds),
                     "-hls_list_size", str(spec.hls_window_segments),
                     "-hls_flags", "delete_segments+append_list", playlist]
            wrote_output = True
        if "record" in spec.outputs:
            ext = {"fmp4": "mp4", "mp4": "mp4", "mkv": "mkv"}.get(spec.record_format, "mp4")
            seg = os.path.join(spec.output_dir, f"rec_%08d.{ext}")
            args += ["-map", "0:v?", "-map", "0:a?", "-c", "copy", "-f", "segment",
                     "-segment_time", str(spec.record_segment_seconds),
                     "-reset_timestamps", "1", seg]
            wrote_output = True
        if not wrote_output:
            # snapshot/thumbnail-only session: keep the connection open writing periodic frames.
            args += ["-map", "0:v?", "-f", "image2", "-r", "1/5",
                     os.path.join(spec.output_dir, "thumb_%08d.jpg")]

        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        self._seq += 1
        hid = f"ff-{self._seq}"
        self._procs[hid] = {"proc": proc, "dir": spec.output_dir, "started": time.time()}
        return EngineHandle(id=hid, pid=proc.pid, output_dir=spec.output_dir)

    async def stop(self, handle: EngineHandle) -> None:
        entry = self._procs.pop(handle.id, None)
        if entry is None:
            return
        proc = entry["proc"]
        if proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass

    async def stats(self, handle: EngineHandle) -> EngineStats:
        entry = self._procs.get(handle.id)
        if entry is None:
            return EngineStats(alive=False)
        proc = entry["proc"]
        alive = proc.returncode is None
        total = 0
        newest = 0.0
        try:
            for name in os.listdir(entry["dir"]):
                p = os.path.join(entry["dir"], name)
                st = os.stat(p)
                total += st.st_size
                newest = max(newest, st.st_mtime)
        except OSError:
            pass
        return EngineStats(bytes_out=total, alive=alive,
                           last_frame_at=(_iso(newest) if newest else None))

    async def is_alive(self, handle: EngineHandle) -> bool:
        entry = self._procs.get(handle.id)
        return bool(entry) and entry["proc"].returncode is None

    async def snapshot(self, source_url: str, *, transport: str = "tcp",
                       max_width: int = 1280) -> bytes:
        args = [self._ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error"]
        if source_url.startswith("rtsp"):
            args += ["-rtsp_transport", "udp" if transport == "udp" else "tcp"]
        args += ["-i", source_url, "-frames:v", "1",
                 "-vf", f"scale='min({max_width},iw)':-2", "-f", "image2", "-c:v", "mjpeg", "pipe:1"]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if proc.returncode != 0 or not out:
            raise RuntimeError("snapshot failed")
        return out

    async def probe(self, source_url: str, *, transport: str = "tcp") -> ProbeResult:
        args = [self._ffprobe, "-hide_banner", "-loglevel", "error",
                "-print_format", "json", "-show_streams"]
        if source_url.startswith("rtsp"):
            args += ["-rtsp_transport", "udp" if transport == "udp" else "tcp"]
        args += [source_url]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        out, _ = await proc.communicate()
        if proc.returncode != 0 or not out:
            return ProbeResult(ok=False, error="probe failed")
        try:
            streams = json.loads(out).get("streams", [])
        except Exception:   # noqa: BLE001
            return ProbeResult(ok=False, error="unparseable probe output")
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        has_audio = any(s.get("codec_type") == "audio" for s in streams)
        return ProbeResult(
            codec=video.get("codec_name", ""), width=int(video.get("width") or 0),
            height=int(video.get("height") or 0), fps=_fps(video.get("r_frame_rate", "0/1")),
            has_audio=has_audio, ptz_capable=False, ok=True)


def _sibling(ffmpeg_path: str, name: str) -> str:
    d = os.path.dirname(ffmpeg_path)
    exe = name + (".exe" if ffmpeg_path.lower().endswith(".exe") else "")
    return os.path.join(d, exe) if d else name


def _fps(r: str) -> float:
    try:
        num, den = r.split("/")
        return round(int(num) / int(den), 3) if int(den) else 0.0
    except Exception:   # noqa: BLE001
        return 0.0


def _iso(mtime: float) -> str:
    import datetime as _dt
    return _dt.datetime.fromtimestamp(mtime, _dt.timezone.utc).isoformat()
