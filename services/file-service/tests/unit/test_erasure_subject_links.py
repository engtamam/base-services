"""Erasure must reach a person's data however it got there — and never claim it did not exist.

THE BUG. FileErasure found a subject's files with find(files, {"tenant", "owner": subject}).
That is right only while the uploader IS the data subject. When a service stores data on
someone's behalf — a recording sink writing match footage as "service:stream" — the owner is the
service and the subject is named nowhere on the record.

So a DSAR for a child in that footage matched zero rows and returned

    {"erased": [], "already_absent": true}

which is not "we could not find them". It is an assertion that this service holds nothing about
that person, filed and produced as evidence a right was honoured. A missing delete path gets
noticed the first time somebody uses it; a false receipt does not.
"""
from __future__ import annotations

import asyncio

from app.core.erasure import FileErasure
from app.core.pii import subject_ref
from app.core.subject_links import SubjectLinks

SALT = "s" * 32
TENANT = "acme"
CHILD = "child@example.com"


class _Store:
    """In-memory DataStore: get/put/delete/find/incr/del_if_zero, equality filters only."""

    def __init__(self):
        self.data: dict[str, dict[str, dict]] = {}

    async def put(self, coll, key, value):
        self.data.setdefault(coll, {})[key] = dict(value)

    async def get(self, coll, key):
        return self.data.get(coll, {}).get(key)

    async def delete(self, coll, key):
        return self.data.get(coll, {}).pop(key, None) is not None

    async def find(self, coll, flt, limit=500, after=None, sort=None):
        rows = [r for r in self.data.get(coll, {}).values()
                if all(r.get(k) == v for k, v in flt.items())]
        return rows, None

    async def incr(self, coll, key, delta):
        row = self.data.setdefault(coll, {}).setdefault(key, {"count": 0})
        row["count"] += delta
        return row["count"]

    async def del_if_zero(self, coll, key):
        row = self.data.get(coll, {}).get(key)
        if row and row.get("count", 0) == 0:
            del self.data[coll][key]
            return True
        return False


class _Objects:
    def __init__(self):
        self.deleted = []

    async def delete(self, sha):
        self.deleted.append(sha)


async def _store_footage(store, *, file_id, owner="service:stream", sha="sha_match"):
    """A recording written by a SERVICE. Note the owner: the child is nowhere on this record."""
    await store.put("files", file_id,
                    {"id": file_id, "tenant": TENANT, "owner": owner,
                     "filename": "match.mp4", "sha256": sha})
    await store.put("blob_refs", sha, {"count": 1})


def test_the_receipt_no_longer_claims_absence_when_footage_exists():
    """THE REGRESSION. Before the fix this returned already_absent=true with the video intact."""
    async def _body():
        store, objects = _Store(), _Objects()
        await _store_footage(store, file_id="file_1")
        await SubjectLinks(store=store, pii_salt=SALT).attach(
            tenant=TENANT, file_id="file_1", subjects=[CHILD])

        ack = await FileErasure(store=store, object_store=objects, pii_salt=SALT).erase(TENANT, CHILD)

        assert ack["already_absent"] is False, "the receipt asserted the child had no data here"
        assert "file_1" not in store.data["files"], "the footage survived its own erasure"
        assert "sha_match" in objects.deleted, "the bytes were left in the bucket"
        assert any("linked_files" in e for e in ack["erased"])


    asyncio.run(_body())

def test_the_linkage_itself_is_erased():
    """That a named child appeared in a recording is the fact she most likely wanted removed."""
    async def _body():
        store = _Store()
        await _store_footage(store, file_id="file_1")
        links = SubjectLinks(store=store, pii_salt=SALT)
        await links.attach(tenant=TENANT, file_id="file_1", subjects=[CHILD])

        await FileErasure(store=store, object_store=_Objects(), pii_salt=SALT).erase(TENANT, CHILD)

        ref = subject_ref(SALT, TENANT, CHILD)
        assert await links.files_for(tenant=TENANT, ref=ref) == []
        assert store.data.get("file_subjects", {}) == {}


    asyncio.run(_body())

def test_a_file_shared_with_another_person_does_not_vanish_for_them():
    """The ref-count rule must hold on the linked path exactly as it does for owned files."""
    async def _body():
        store, objects = _Store(), _Objects()
        await _store_footage(store, file_id="file_1", sha="shared")
        await store.put("blob_refs", "shared", {"count": 2})      # a second file references it
        await store.put("files", "file_2",
                        {"id": "file_2", "tenant": TENANT, "owner": "someone", "sha256": "shared"})
        await SubjectLinks(store=store, pii_salt=SALT).attach(
            tenant=TENANT, file_id="file_1", subjects=[CHILD])

        await FileErasure(store=store, object_store=objects, pii_salt=SALT).erase(TENANT, CHILD)

        assert "file_2" in store.data["files"]
        assert objects.deleted == [], "deleted a blob the other owner still references"


    asyncio.run(_body())

def test_owned_files_still_erase_exactly_as_before():
    """The original path must not regress: this is what every existing adopter relies on."""
    async def _body():
        store, objects = _Store(), _Objects()
        await store.put("files", "own_1",
                        {"id": "own_1", "tenant": TENANT, "owner": CHILD, "sha256": "sha_own"})
        await store.put("blob_refs", "sha_own", {"count": 1})

        ack = await FileErasure(store=store, object_store=objects, pii_salt=SALT).erase(TENANT, CHILD)

        assert "own_1" not in store.data["files"]
        assert "sha_own" in objects.deleted
        assert "files:1" in ack["erased"]


    asyncio.run(_body())

def test_a_genuinely_absent_subject_still_acks_absent():
    """The honest case must stay honest — widening the search must not invent data."""
    async def _body():
        ack = await FileErasure(store=_Store(), object_store=_Objects(),
                                pii_salt=SALT).erase(TENANT, "nobody@example.com")
        assert ack["already_absent"] is True
        assert ack["erased"] == []


    asyncio.run(_body())

def test_erasure_is_idempotent_across_both_paths():
    """A DSAR retries. The second run must not fail on links whose files are already gone."""
    async def _body():
        store = _Store()
        await _store_footage(store, file_id="file_1")
        await SubjectLinks(store=store, pii_salt=SALT).attach(
            tenant=TENANT, file_id="file_1", subjects=[CHILD])
        eraser = FileErasure(store=store, object_store=_Objects(), pii_salt=SALT)

        await eraser.erase(TENANT, CHILD)
        second = await eraser.erase(TENANT, CHILD)
        assert second["already_absent"] is True


    asyncio.run(_body())

def test_linking_the_same_subject_twice_is_one_link():
    """A retried identification job must not make one file count three times in a receipt."""
    async def _body():
        store = _Store()
        links = SubjectLinks(store=store, pii_salt=SALT)
        for _ in range(3):
            await links.attach(tenant=TENANT, file_id="file_1", subjects=[CHILD])
        ref = subject_ref(SALT, TENANT, CHILD)
        assert await links.files_for(tenant=TENANT, ref=ref) == ["file_1"]


    asyncio.run(_body())

def test_the_linkage_stores_a_pseudonym_never_the_person():
    """A table of raw email addresses would be a new copy of what erasure exists to remove."""
    async def _body():
        store = _Store()
        await SubjectLinks(store=store, pii_salt=SALT).attach(
            tenant=TENANT, file_id="file_1", subjects=[CHILD])
        blob = repr(store.data["file_subjects"])
        assert CHILD not in blob
        assert subject_ref(SALT, TENANT, CHILD) in blob


    asyncio.run(_body())

def test_one_tenants_link_never_reaches_another_tenants_subject():
    async def _body():
        store = _Store()
        links = SubjectLinks(store=store, pii_salt=SALT)
        await links.attach(tenant=TENANT, file_id="file_1", subjects=[CHILD])
        other = subject_ref(SALT, "globex", CHILD)
        assert await links.files_for(tenant="globex", ref=other) == []

    asyncio.run(_body())
