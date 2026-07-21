"""FileServiceRecordingSink — store recording segments in file-service.

Recordings are just objects, and file-service already does scanned, content-addressed, retained,
presign-served object storage (Phase 34). So a segment is POSTed there and we keep only the returned
file id; download is a file-service signed URL (bytes served by file-service, never by us). This is
the plug-and-play payoff — stream-service does not reimplement blob storage, it composes the service
that owns it.

The HTTP calls go through an injectable `transport` so the logic is unit-tested without a live
file-service; the default transport is httpx with a service token (files.upload).
"""
from __future__ import annotations

from typing import Any, Callable, Awaitable

from app.ports.stream_types import SegmentRef

# transport(method, url, headers, content) -> (status_code, json_or_none)
Transport = Callable[[str, str, dict, bytes | None], Awaitable[tuple[int, Any]]]


class FileServiceRecordingSink:
    driver = "file-service"

    def __init__(self, base_url: str, token: str, *, transport: Transport | None = None,
                 timeout: float = 30.0, token_resolver=None) -> None:
        if not base_url:
            raise ValueError("RECORDING_FILE_SERVICE_URL is required for the file-service sink")
        self._base = base_url.rstrip("/")
        self._token = token
        # Read at use time rather than captured here, so an operator can replace the upload token
        # without a redeploy. Absent (no KEK configured), the constructor value is used exactly as
        # before.
        self._token_resolver = token_resolver
        self._timeout = timeout
        self._transport = transport or self._httpx_transport

    async def _auth(self) -> dict:
        token = await self._token_resolver.get() if self._token_resolver else self._token
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def put_segment(self, *, tenant: str, session_id: str, seq: int, data: bytes,
                          content_type: str, start: str, end: str) -> SegmentRef:
        # file-service takes a raw-body upload with the filename in a header.
        headers = {**await self._auth(), "Content-Type": content_type,
                   "X-Filename": f"{session_id}_{seq:08d}.mp4"}
        status, body = await self._transport("POST", f"{self._base}/api/v1/files", headers, data)
        if status not in (200, 201) or not isinstance(body, dict) or "id" not in body:
            raise RuntimeError(f"file-service upload failed: {status}")
        return SegmentRef(ref=body["id"], bytes=len(data), start=start, end=end)

    async def delete(self, ref: str) -> bool:
        status, _ = await self._transport("DELETE", f"{self._base}/api/v1/files/{ref}",
                                          await self._auth(), None)
        return status in (200, 204)

    async def signed_url(self, ref: str, *, ttl: int) -> str:
        status, body = await self._transport("POST", f"{self._base}/api/v1/files/{ref}/signed-url",
                                             await self._auth(), None)
        if status == 200 and isinstance(body, dict) and body.get("url"):
            # file-service returns a relative path; the caller prefixes the file-service origin.
            return f"{self._base}{body['url']}"
        raise RuntimeError(f"file-service signed-url failed: {status}")

    async def _httpx_transport(self, method: str, url: str, headers: dict,
                               content: bytes | None):   # pragma: no cover - needs a live file-service
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.request(method, url, headers=headers, content=content)
        try:
            body = resp.json()
        except Exception:   # noqa: BLE001
            body = None
        return resp.status_code, body
