"""The httpx transport must CAP the response body during the read, not after.

A hostile or broken receiver can return an unbounded body. `client.post(...).text` buffers the whole
thing into memory before truncating — an OOM. The transport streams and stops pulling once it has
enough to store. This fakes httpx to prove both: the stored body is capped, AND the stream is not
drained past the cap.
"""
from __future__ import annotations

from app.adapters.transport.httpx_transport import HttpxTransport


class _FakeResp:
    def __init__(self, chunks, counter):
        self.status_code = 200
        self.headers = {"Content-Type": "text/plain"}
        self._chunks = chunks
        self._counter = counter

    def iter_raw(self):
        for c in self._chunks:
            self._counter["pulled"] += len(c)
            yield c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _FakeClient:
    def __init__(self, resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def stream(self, method, url, **kw):
        return self._resp


class _FakeHttpx:
    def __init__(self, resp):
        self._resp = resp

    def HTTPTransport(self, **kw):
        return object()

    def Client(self, **kw):
        return _FakeClient(self._resp)


def test_body_is_capped_and_the_stream_is_not_drained_past_the_cap():
    cap = 1000
    counter = {"pulled": 0}
    # 100 chunks of 1KB = ~100KB available; the transport must stop after ~cap bytes.
    chunks = [b"x" * 1024 for _ in range(100)]
    t = HttpxTransport()
    t._httpx = _FakeHttpx(_FakeResp(chunks, counter))

    resp = t.send(url="https://shop.example/hook", pinned_ip="93.184.216.34",
                  body=b"{}", headers={}, timeout_seconds=5, max_response_bytes=cap)

    assert resp.ok and resp.status == 200
    assert len(resp.body) == cap                       # stored body capped
    # Early stop: it read one chunk past the cap at most, never the whole 100KB.
    assert counter["pulled"] <= cap + 1024
