"""WhatsAppMediaUploader port — turn a media file into the handle a carousel template needs.

A carousel (and other media-header templates) must be created with an EXAMPLE media handle per card,
and Meta only accepts a handle produced by its Resumable Upload API — not a URL, not a raw file. So a
caller building a carousel template would otherwise have to run Meta's two-step upload itself before
every template create. This port owns that lifecycle instead: give it the bytes + MIME type, get back
the `h` handle to drop into `header_example_handle`.

Verified against Meta's Resumable Upload API (Sep 2026): POST /{app_id}/uploads?file_length&file_type
&file_name&access_token → {"id":"upload:<session>"}; then POST /{session} with headers
`Authorization: OAuth <token>` + `file_offset: 0` and the raw bytes → {"h":"<handle>"}.

The `console` driver returns a deterministic fake handle so the whole carousel lifecycle runs in dev
with no Meta round-trip; the `meta` driver performs the real two-step upload.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class UploadResult:
    ok: bool
    handle: str = ""      # the `h` value — goes into a card's header_example_handle at template create
    reason: str = ""


@dataclass(frozen=True)
class MediaIdResult:
    ok: bool
    media_id: str = ""    # the media id — referenced at SEND time in a card header (image.id / video.id)
    reason: str = ""


@runtime_checkable
class WhatsAppMediaUploader(Protocol):
    driver: str

    def upload_handle(self, *, file_bytes: bytes, file_type: str,
                      file_name: str = "") -> UploadResult:
        """Upload the bytes via Meta's Resumable Upload API and return the reusable file HANDLE.

        The handle is for TEMPLATE CREATION (a card's header_example_handle). It is NOT a send-time
        media id — sending references media by the id from upload_media_id() instead."""
        ...

    def upload_media_id(self, *, file_bytes: bytes, file_type: str, phone_number_id: str,
                        file_name: str = "") -> MediaIdResult:
        """Upload the bytes to /{phone_number_id}/media and return the MEDIA ID used at SEND time.

        Distinct from upload_handle: creation needs an App-scoped resumable-upload handle; a send needs
        a phone-number-scoped media id. Same file, two different Meta endpoints and two different
        identifiers — so a carousel that is created AND sent uploads its media through both."""
        ...
