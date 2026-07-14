"""The content-addressed ref-count must not lose a customer's bytes under concurrency.

Two races, both reported as our P0 shape (a row/sha/manifest pointing at bytes nobody has):
  1. Two concurrent uploads of the SAME content lose an increment -> count undershoots -> the blob
     is deleted while an owner still references it.
  2. A delete racing a fresh upload of the same content deletes the newcomer's bytes.
"""
from __future__ import annotations

import asyncio

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.core.file_repository import FileRepository

_REFS = "blob_refs"


def run(c):
    return asyncio.run(c)


# ── the atomic primitives ────────────────────────────────────────────────────
def test_incr_is_atomic_under_concurrency():
    """1000 concurrent +1 must land exactly 1000 — a get()+put() ref-count loses some."""
    async def go():
        s = MemoryDataStore()
        await asyncio.gather(*[s.incr(_REFS, "sha", 1) for _ in range(1000)])
        return (await s.get(_REFS, "sha"))["count"]
    assert run(go()) == 1000


def test_incr_never_goes_negative():
    async def go():
        s = MemoryDataStore()
        await s.incr(_REFS, "k", 1)
        await asyncio.gather(*[s.incr(_REFS, "k", -1) for _ in range(10)])
        return (await s.get(_REFS, "k"))["count"]
    assert run(go()) == 0


def test_del_if_zero_only_deletes_a_zero_row():
    async def go():
        s = MemoryDataStore()
        await s.incr(_REFS, "k", 2)
        assert await s.del_if_zero(_REFS, "k") is False    # count 2 -> not deleted
        await s.incr(_REFS, "k", -1)
        await s.incr(_REFS, "k", -1)                        # now 0
        assert await s.del_if_zero(_REFS, "k") is True      # deleted
        assert await s.del_if_zero(_REFS, "k") is False     # already gone
    run(go())


def test_del_if_zero_lets_exactly_one_caller_claim():
    """Two deleters racing on the same zeroed row: exactly ONE may claim the blob delete."""
    async def go():
        s = MemoryDataStore()
        await s.incr(_REFS, "k", 0)                         # a row at 0
        wins = await asyncio.gather(*[s.del_if_zero(_REFS, "k") for _ in range(20)])
        return sum(wins)
    assert run(go()) == 1


# ── the two races, end to end through the repo ───────────────────────────────
class _Objects:
    """A tiny content-addressed blob store, tracking what physically exists."""
    def __init__(self):
        self.blobs: dict[str, bytes] = {}

    async def put(self, key, data, ct=None):
        self.blobs[key] = data

    async def get(self, key):
        return self.blobs[key]

    async def delete(self, key):
        self.blobs.pop(key, None)
        return True


async def _upload(store, objects, sha, data):
    """Mirror the service's upload ordering: blob FIRST, then increment the ref."""
    await objects.put(sha, data)
    await FileRepository(store).incr_ref(sha)


async def _delete(store, objects, sha):
    """Mirror the service's delete: decrement, then delete the blob ONLY under the claim."""
    repo = FileRepository(store)
    if await repo.decr_ref(sha) == 0 and await repo.claim_blob_delete(sha):
        await objects.delete(sha)


def test_two_uploads_then_one_delete_keeps_the_blob_for_the_other_owner():
    """Both A and B upload the same content, then A deletes. B still holds it — the blob must
    survive. With a lost-increment count of 1, A's delete would have wiped B's bytes."""
    async def go():
        s, o = MemoryDataStore(), _Objects()
        sha = "deadbeef"
        await asyncio.gather(_upload(s, o, sha, b"iso"), _upload(s, o, sha, b"iso"))
        await _delete(s, o, sha)                            # A removes his copy
        return sha in o.blobs, (await s.get(_REFS, sha) or {}).get("count")
    present, count = run(go())
    assert present is True, "B still references the blob — it must not have been deleted"
    assert count == 1


def test_delete_racing_a_fresh_upload_never_loses_the_newcomers_bytes():
    """A holds the only copy and deletes it; B uploads the identical content at the same time.
    However the two interleave, the blob B relies on must exist at the end."""
    async def go():
        losses = 0
        for _ in range(200):                               # many interleavings
            s, o = MemoryDataStore(), _Objects()
            sha = "cafe"
            await _upload(s, o, sha, b"x")                  # A's initial copy (count 1)
            await asyncio.gather(_delete(s, o, sha),        # A deletes
                                 _upload(s, o, sha, b"x"))  # B uploads concurrently
            # B's upload incremented the ref, so B references the blob and it must be present.
            if (await s.get(_REFS, sha) or {}).get("count", 0) >= 1 and sha not in o.blobs:
                losses += 1
        return losses
    assert run(go()) == 0, "a referenced blob was deleted — bytes lost for a live owner"
