"""ffmpeg exporter — concatenate the overlapping segments and remux them into one clip.

REMUX, NOT RE-ENCODE, by default. `-c copy` moves the same bitstream into a new container: fast
(no decode/encode), lossless, and it cannot be accused of degrading what a camera recorded. That
last point is the one that matters for a VMS — a clip pulled for an incident review should be the
footage, not a re-rendering of it. Re-encoding is available through EXPORT_ARGS for platforms that
need a smaller file, and it is opt-in precisely because it changes the pixels.

HOW IT IS INVOKED, and why each part is the way it is:

  * A LIST argv, never a shell string. Segment URLs are signed and come from our own sink, but
    building a command line by string concatenation is how a filename with a quote in it becomes
    remote code execution. There is no shell in this path at all.
  * A concat DEMUXER list file, not the `concat:` protocol. The protocol only works for a couple
    of container formats and silently produces a broken file for the rest; the demuxer handles
    fragmented MP4, which is what RECORD_FORMAT defaults to.
  * `-protocol_whitelist` restricted to what a signed URL actually needs. Without it ffmpeg will
    happily follow whatever a list file points at, which turns a segment reference into an
    arbitrary-file-read.
  * A hard timeout, and the process is KILLED on it. An export that hangs would otherwise hold a
    worker for ever, and one stuck export must not stop every other tenant's.
  * Output is written to a temp file and read back, rather than piped. Piping a multi-gigabyte
    remux through a pipe deadlocks the moment the reader is slower than ffmpeg.

The binary path, the container, the codec arguments and the timeout are ALL configuration. A
platform that ships a different ffmpeg build, or needs `-movflags +faststart` for web playback,
changes config rather than this file.
"""
from __future__ import annotations

import asyncio
import os
import pathlib
import shutil
import tempfile

from app.ports.clip_exporter import FAILED, READY, ClipResult, ClipSource


class FfmpegClipExporter:
    driver = "ffmpeg"

    def __init__(self, *, binary: str, timeout_seconds: int, args_by_format: dict[str, list[str]],
                 content_types: dict[str, str], protocols: str) -> None:
        self._bin = binary
        self._timeout = timeout_seconds
        self._args = dict(args_by_format)
        self._types = dict(content_types)
        self._protocols = protocols

    async def export(self, *, sources: list[ClipSource], fmt: str,
                     start: str, end: str) -> ClipResult:
        if not sources:
            # Not an error in the transport sense, but absolutely a failed export: the caller
            # asked for a window this camera has no footage for, and saying "ready" with an empty
            # file would be the silent-hole failure this port exists to prevent.
            return ClipResult(status=FAILED, detail="no recorded segments overlap that window")
        if shutil.which(self._bin) is None and not pathlib.Path(self._bin).is_file():
            return ClipResult(status=FAILED,
                              detail=f"EXPORT_FFMPEG_BIN {self._bin!r} is not executable here")

        args = self._args.get(fmt)
        if args is None:
            return ClipResult(status=FAILED,
                              detail=f"no EXPORT_ARGS entry for format {fmt!r}")

        workdir = tempfile.mkdtemp(prefix="clip-")
        try:
            listing = pathlib.Path(workdir) / "segments.txt"
            # Single quotes escaped per the concat demuxer's own rules. Signed URLs are ours, but
            # the escaping is not optional: one unescaped quote silently truncates the list, and a
            # SHORTER clip that still reports success is exactly the failure mode we refuse.
            listing.write_text(
                "\n".join(f"file '{s.url.replace(chr(39), chr(39) + chr(92) + chr(39) + chr(39))}'"
                          for s in sources),
                encoding="utf-8")
            out = pathlib.Path(workdir) / f"clip.{fmt}"

            argv = [self._bin, "-nostdin", "-hide_banner", "-loglevel", "error",
                    "-protocol_whitelist", self._protocols,
                    "-f", "concat", "-safe", "0", "-i", str(listing),
                    *args, str(out)]

            proc = await asyncio.create_subprocess_exec(  # nosec B603 - argv list, no shell
                *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                _, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return ClipResult(status=FAILED,
                                  detail=f"export exceeded EXPORT_TIMEOUT_SECONDS ({self._timeout}s)")

            if proc.returncode != 0:
                # ffmpeg's stderr can carry a path. Truncated and passed through as DETAIL rather
                # than raised, so an operator can diagnose without the message reaching a client.
                tail = (stderr or b"").decode("utf-8", "replace").strip().splitlines()[-1:]
                return ClipResult(status=FAILED,
                                  detail=f"ffmpeg exited {proc.returncode}: {' '.join(tail)[:200]}")
            if not out.exists() or out.stat().st_size == 0:
                # A clean exit is not proof of a clip. This is the "ready but unopenable" failure,
                # and it is the one a customer discovers instead of us.
                return ClipResult(status=FAILED, detail="ffmpeg exited 0 but produced no output")

            data = out.read_bytes()
            return ClipResult(status=READY, data=data,
                              content_type=self._types.get(fmt, "application/octet-stream"),
                              meta={"segments": len(sources), "bytes": len(data)})
        except OSError as exc:
            return ClipResult(status=FAILED, detail=f"export failed: {type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
