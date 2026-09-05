"""Console media uploader — a deterministic fake handle for dev. No Meta round-trip.

Lets the whole carousel lifecycle run with zero infra: upload → handle → register template → submit.
The meta driver performs the real two-step Resumable Upload.
"""
from __future__ import annotations

import hashlib

from app.ports.whatsapp_media_uploader import MediaIdResult, UploadResult


class ConsoleMediaUploader:
    driver = "console"

    def upload_handle(self, *, file_bytes: bytes, file_type: str,
                      file_name: str = "") -> UploadResult:
        if not file_bytes:
            return UploadResult(ok=False, reason="no file bytes to upload")
        # A handle stable for identical bytes, so a dev re-run of the same asset is idempotent-ish and
        # a test can assert on it without a network call.
        digest = hashlib.sha256(file_bytes).hexdigest()[:16]
        return UploadResult(ok=True, handle=f"console-handle:{digest}")

    def upload_media_id(self, *, file_bytes: bytes, file_type: str, phone_number_id: str,
                        file_name: str = "") -> MediaIdResult:
        if not file_bytes:
            return MediaIdResult(ok=False, reason="no file bytes to upload")
        digest = hashlib.sha256(file_bytes).hexdigest()[:16]
        return MediaIdResult(ok=True, media_id=f"console-media:{digest}")
