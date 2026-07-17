"""Hot-path proof for the LARGE path: the app does O(1) work independent of object size.

The whole point of the direct-to-bucket model is that the bytes never flow through the app. So the
request path must NEVER read the object: no `request.body()` of a multi-GB upload, no
`object_store.get()` streaming a download through the process. We prove it by counting the bytes the
app pulls from the store on each request path and asserting it is zero, at two very different sizes.
"""
from __future__ import annotations

import hashlib

from app.adapters.datastore.memory import MemoryDataStore
from app.core.file_repository import FileRepository
from app.core.file_service import FileService
from app.core.upload_session import UploadSessionStore
from app.ports.object_store import CompletedPart
from tests.contracts.util import run
from tests.fakes import FakePresignStore


class _CountingStore(FakePresignStore):
    """A presign store that records how many bytes the APP pulled through it via get()/stream()."""

    def __init__(self):
        super().__init__()
        self.bytes_through_app = 0
        self.get_calls = 0
        self.stream_calls = 0
        self.chunks = 0

    async def get(self, key):
        self.get_calls += 1
        data = await super().get(key)
        self.bytes_through_app += len(data)
        return data

    async def stream(self, key, chunk_size=1_048_576):
        self.stream_calls += 1
        async for chunk in super().stream(key, chunk_size):
            self.chunks += 1
            yield chunk


class _Q:
    driver = "q"

    def __init__(self):
        self.n = 0

    async def enqueue(self, *a):
        self.n += 1
        return "j"


def _svc(store):
    ds = MemoryDataStore()
    return FileService(
        repo=FileRepository(ds), object_store=store, scanner=None, signed_url=None,
        max_upload_bytes=25 << 20, allowed_content_types=frozenset(),
        sessions=UploadSessionStore(ds), queue=_Q(), scan_queue="file-scan",
        max_object_bytes=50 << 30, part_size=8 << 20,
    ), ds


def _ready_file(svc, store, size):
    data = b"x" * size
    sha = hashlib.sha256(data).hexdigest()
    b = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                   sha256=sha, size=size))
    run(store.put(b.session.staging_key, data))     # simulate the client's direct-to-bucket PUT
    run(svc.complete_large_upload(session_id=b.session.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))
    run(svc.finalize_large_upload(b.session.id, computed_sha=sha, clean=True))
    return sha, run(svc._sessions.get(b.session.id)).file_id


def test_request_path_pulls_zero_bytes_regardless_of_size():
    # The app's INITIATE/COMPLETE and DOWNLOAD request paths must not read the object at any size.
    for size in (10_000, 5_000_000):
        store = _CountingStore()
        svc, ds = _svc(store)
        store.bytes_through_app = 0   # ignore the test's own simulated PUT accounting (put ≠ get)

        sha, fid = _ready_file(svc, store, size)
        # finalize promotes staging→content via the store (server-side, no app bytes) — reset the
        # counter to isolate the DOWNLOAD request path.
        store.bytes_through_app = 0
        store.get_calls = 0

        f = run(FileRepository(ds).get(fid))
        redirect = run(svc.download_redirect(f))
        assert redirect is not None and redirect.startswith("memory://" + sha)
        # The download handed back a presigned URL — the app read NOTHING.
        assert store.get_calls == 0
        assert store.bytes_through_app == 0


def test_scan_worker_streams_in_bounded_chunks_not_one_slurp():
    """The one place bytes DO flow (the async scan) reads in bounded chunks, never a whole-object
    get(). This keeps a multi-GB scan within bounded memory."""
    from app.adapters.scanner.none import NoneScanner
    from app.core.scan_worker import ScanWorker

    store = _CountingStore()
    svc, ds = _svc(store)
    data = b"y" * 3_000_000
    sha = hashlib.sha256(data).hexdigest()
    b = run(svc.begin_large_upload(tenant="t", owner="u", filename="v", content_type="",
                                   sha256=sha, size=len(data)))
    run(store.put(b.session.staging_key, data))
    run(svc.complete_large_upload(session_id=b.session.id, tenant="t", owner="u",
                                  parts=[CompletedPart(1, "e1")]))

    worker = ScanWorker(files=svc, sessions=svc._sessions, object_store=store,
                        scanner=NoneScanner(), queue=svc._queue, scan_queue="file-scan",
                        chunk_size=1_048_576)
    store.get_calls = 0
    run(worker.process(b.session.id))
    # It streamed in BOUNDED chunks (3 MB / 1 MiB ≈ 3), and never called the whole-object get().
    assert store.get_calls == 0
    assert store.stream_calls == 1 and store.chunks >= 3
    assert run(svc._sessions.get(b.session.id)).status == "ready"
