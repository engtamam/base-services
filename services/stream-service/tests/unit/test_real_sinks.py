"""The real recording sinks — file-service (HTTP, injectable) and s3 (ObjectStore-backed)."""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.stream.file_service_sink import FileServiceRecordingSink
from app.adapters.stream.recording_sink import S3RecordingSink


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── file-service sink ──────────────────────────────────────────────────────────
class _FakeFileService:
    """A stand-in file-service: records requests, returns a file id on upload."""
    def __init__(self):
        self.uploaded = {}
        self.deleted = []
        self._n = 0

    async def __call__(self, method, url, headers, content):
        assert headers.get("Authorization") == "Bearer svc-token"   # the sink sends its service token
        if method == "POST" and url.endswith("/api/v1/files"):
            self._n += 1
            fid = f"file{self._n}"
            self.uploaded[fid] = content
            return 201, {"id": fid}
        if method == "DELETE":
            self.deleted.append(url.rsplit("/", 1)[1])
            return 204, None
        if method == "POST" and url.endswith("/signed-url"):
            return 200, {"url": "/files/signed/tok123"}
        return 500, None


def test_file_service_sink_uploads_and_returns_the_file_id():
    fs = _FakeFileService()
    sink = FileServiceRecordingSink("http://file-service:8000", "svc-token", transport=fs)
    ref = run(sink.put_segment(tenant="t", session_id="s", seq=3, data=b"seg-bytes",
                               content_type="video/mp4", start="a", end="b"))
    assert ref.ref == "file1" and ref.bytes == 9
    assert fs.uploaded["file1"] == b"seg-bytes"           # the bytes went to file-service, not local


def test_file_service_sink_delete_and_signed_url():
    fs = _FakeFileService()
    sink = FileServiceRecordingSink("http://file-service:8000", "svc-token", transport=fs)
    assert run(sink.delete("file1")) is True and fs.deleted == ["file1"]
    url = run(sink.signed_url("file1", ttl=60))
    assert url == "http://file-service:8000/files/signed/tok123"   # origin-prefixed


def test_file_service_sink_needs_a_base_url():
    with pytest.raises(ValueError):
        FileServiceRecordingSink("", "tok")


def test_file_service_sink_raises_on_upload_failure():
    async def _fail(method, url, headers, content):
        return 500, None
    sink = FileServiceRecordingSink("http://fs", "t", transport=_fail)
    with pytest.raises(RuntimeError):
        run(sink.put_segment(tenant="t", session_id="s", seq=1, data=b"x",
                             content_type="video/mp4", start="a", end="b"))


# ── s3 sink ────────────────────────────────────────────────────────────────────
class _FakeObjectStore:
    driver = "s3"
    supports_presign = True
    def __init__(self): self.data = {}
    async def put(self, key, data, content_type=None): self.data[key] = bytes(data)
    async def delete(self, key): return self.data.pop(key, None) is not None
    async def presign_get(self, key, expires_in):
        return f"https://bucket.example/{key}?X-Expires={expires_in}"


def test_s3_sink_stores_by_key_and_deletes():
    store = _FakeObjectStore()
    sink = S3RecordingSink(store)
    ref = run(sink.put_segment(tenant="t1", session_id="sess", seq=5, data=b"video",
                               content_type="video/mp4", start="a", end="b"))
    assert ref.ref == "rec/t1/sess/00000005.mp4"
    assert store.data[ref.ref] == b"video"
    assert run(sink.delete(ref.ref)) is True and ref.ref not in store.data


def test_s3_sink_signed_url_presigns_through_the_store():
    # THE FIX. This used to raise NotImplementedError, so an s3-sink deployment could store and
    # erase footage but never EXPORT it — export needs a download URL per segment.
    store = _FakeObjectStore()
    sink = S3RecordingSink(store)
    url = run(sink.signed_url("rec/t1/sess/00000000.mp4", ttl=300))
    assert url == "https://bucket.example/rec/t1/sess/00000000.mp4?X-Expires=300"


def test_s3_sink_signed_url_on_a_store_that_cannot_presign_is_honest():
    from app.ports.errors import Unsupported

    class _NoPresign(_FakeObjectStore):
        supports_presign = False
        async def presign_get(self, key, expires_in):
            raise Unsupported("cannot presign")

    sink = S3RecordingSink(_NoPresign())
    with pytest.raises(Unsupported):
        run(sink.signed_url("k", ttl=60))
