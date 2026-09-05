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

from app.adapters.notify_http import _request, ok, post_json
from app.ports.errors import ConfigError
from app.ports.whatsapp_media_uploader import UploadResult


class MetaMediaUploader:
    driver = "meta"

    def __init__(self, access_token: str, app_id: str, base_url: str) -> None:
        if not access_token or not app_id:
            raise ConfigError(
                "WHATSAPP_ACCESS_TOKEN and META_APP_ID are required for meta media upload")
        self._token = access_token
        self._base = base_url.rstrip("/")
        self._app_id = app_id

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


def _parse(raw: str) -> dict:
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _reason(raw: str) -> str:
    err = _parse(raw).get("error")
    return str(err.get("message", "")) if isinstance(err, dict) else ""
