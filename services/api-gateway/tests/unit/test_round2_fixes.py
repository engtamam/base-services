"""Round-2 fixes (api-gateway). Maps to docs/audits/round2/api-gateway.json."""
from __future__ import annotations

import asyncio

from app.adapters.cache.memory import MemoryCache
from app.core.models import Route

from test_gateway_core import FakeForwarder, _gw, make_request, run


# r2-01 — a dot-segment / encoded-traversal path is rejected BEFORE routing/auth (no bypass).
def test_dot_segment_path_is_rejected():
    r = Route(prefix="/public", upstream="http://up", methods=("GET",), auth="optional")
    gw = _gw(r, forwarder=FakeForwarder(status=200, body=b"OK"))
    # `/public/../admin` would authorize as /public but httpx would fetch /admin
    assert run(gw.handle(make_request(path="/public/../admin"))).status_code == 400
    # encoded traversal in the raw target
    assert run(gw.handle(make_request(path="/public/%2e%2e/admin"))).status_code == 400
    assert run(gw.handle(make_request(path="/public/%2fadmin"))).status_code == 400
    # a clean path still works
    assert run(gw.handle(make_request(path="/public/ok"))).status_code == 200


# r2-02 — the memory cache is bounded and evicts least-recently-used entries.
def test_memory_cache_is_bounded_lru():
    c = MemoryCache(max_entries=3)

    async def _go():
        for i in range(3):
            await c.set(f"k{i}", str(i))
        await c.get("k0")                 # touch k0 -> most recently used
        await c.set("k3", "3")            # over the cap -> evict LRU (k1)
        assert await c.get("k1") is None  # evicted
        assert await c.get("k0") == "0"   # survived (recently used)
        assert await c.get("k3") == "3"
    run(_go())
