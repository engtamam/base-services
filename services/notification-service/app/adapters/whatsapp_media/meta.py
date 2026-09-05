"""Meta media upload — the two-step Resumable Upload API that yields a template media handle.

start  → POST /{app_id}/uploads?file_length&file_type&file_name&access_token → {"id":"upload:<sess>"}
finish → POST /{session} with `Authorization: OAuth <token>` + `file_offset: 0` + raw bytes → {"h":…}

The `h` handle is what a carousel card's header_example_handle needs at template creation. App id +
Graph base are config; the access token is the tenant's (resolved in core, never in a route). A
network/HTTP failure is a not-uploaded outcome carrying a reason, never a crash.

Verified against Meta's Resumable Upload API (Sep 2026).
"""
from __future__ import annotations

import json
import urllib.parse
import uuid

from app.adapters.notify_http import _request, bearer, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_media_uploader import MediaIdResult, UploadResult


def _multipart(fields: dict[str, str], file_field: str, filename: str,
               file_type: str, file_bytes: bytes) -> tuple[str, bytes]:
    """Encode a multipart/form-data body by hand (no third-party dependency). Returns (content_type,
    body). Used for /{phone_number_id}/media, which is a genuine file upload, unlike the JSON routes."""
    boundary = f"----bs{uuid.uuid4().hex}"
    crlf = b"\r\n"
    parts: list[bytes] = []
    for name, value in fields.items():
        parts += [f"--{boundary}".encode(), crlf,
                  f'Content-Disposition: form-data; name="{name}"'.encode(), crlf, crlf,
                  str(value).encode(), crlf]
    parts += [f"--{boundary}".encode(), crlf,
              f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"'.encode(),
              crlf, f"Content-Type: {file_type}".encode(), crlf, crlf, file_bytes, crlf,
              f"--{boundary}--".encode(), crlf]
    return f"multipart/form-data; boundary={boundary}", b"".join(parts)


class MetaMediaUploader:
    driver = "meta"

    def __init__(self, access_token: str, app_id: str, base_url: str) -> None:
        if not access_token or not app_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and META_APP_ID are required for meta media upload")
        self._token = access_token
        self._base = base_url.rstrip("/")
        self._app_id = app_id
        self._bearer = bearer(access_token)

    def upload_handle(self, *, file_bytes: bytes, file_type: str,
                      file_name: str = "") -> UploadResult:
        if not file_bytes:
            return UploadResult(ok=False, reason="no file bytes to upload")
        # Step 1 — start a session. Params in the query string; access_token authenticates the start.
        q = urllib.parse.urlencode({
            "file_length": len(file_bytes), "file_type": file_type,
            "file_name": file_name or "upload.bin", "access_token": self._token})
        status, raw = post_json(f"{self._base}/{self._app_id}/uploads?{q}", {})
        session = _parse(raw).get("id", "")
        if not ok(status) or not session:
            return UploadResult(ok=False, reason=_reason(raw) or f"HTTP {status} starting upload")
        # Step 2 — send the bytes. The finish call authenticates with OAuth (not a bearer) and carries
        # the offset; resuming an interrupted upload is a GET for the offset then this same POST — not
        # needed for the small header images/videos a carousel uses, so a single 0-offset PUT suffices.
        status, raw = _request(f"{self._base}/{session}", file_bytes,
                               {"Authorization": f"OAuth {self._token}", "file_offset": "0"})
        handle = _parse(raw).get("h", "")
        if not ok(status) or not handle:
            return UploadResult(ok=False, reason=_reason(raw) or f"HTTP {status} uploading bytes")
        return UploadResult(ok=True, handle=handle)

    def upload_media_id(self, *, file_bytes, file_type, phone_number_id,
                        file_name="") -> MediaIdResult:
        if not file_bytes:
            return MediaIdResult(ok=False, reason="no file bytes to upload")
        if not phone_number_id:
            return MediaIdResult(ok=False, reason="no phone_number_id for this tenant")
        # /{phone_number_id}/media is a real multipart upload (messaging_product + typed file) and
        # returns {"id": "<media-id>"}, referenced at send time as image.id / video.id.
        ctype, body = _multipart({"messaging_product": "whatsapp", "type": file_type},
                                 "file", file_name or "upload.bin", file_type, file_bytes)
        status, raw = _request(f"{self._base}/{phone_number_id}/media", body,
                               {**self._bearer, "Content-Type": ctype})
        media_id = _parse(raw).get("id", "")
        if not ok(status) or not media_id:
            return MediaIdResult(ok=False, reason=_reason(raw) or f"HTTP {status} uploading media")
        return MediaIdResult(ok=True, media_id=str(media_id))


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(raw: str) -> str:
    err = _parse(raw).get("error")
    return str(err.get("message", "")) if isinstance(err, dict) else ""
