"""Erasure — stream. Two failure modes matter, and they pull in opposite directions.

Delete too little and a person's footage survives a DSAR. Delete too much and we destroy OTHER
people's video (and held evidence) to satisfy one request — which is itself a data-protection
failure, not a cautious one. Most of these tests are about the second.
"""
from __future__ import annotations

import asyncio

from app.core.erasure import HOLD_BASIS, NO_ID_BASIS, StreamErasure

_SALT = "s" * 32


def run(c):
    return asyncio.run(c)


class _Store:
    def __init__(self, recordings=(), exports=()):
        self.data = {"recordings": list(recordings), "exports": list(exports)}
        self.deleted = []

    async def find(self, collection, filt, limit=100, after=None, sort=None):
        rows = [r for r in self.data.get(collection, [])
                if all(r.get(k) == v for k, v in filt.items())]
        start = int(after or 0)
        page = rows[start:start + limit]
        nxt = start + len(page)
        return page, (str(nxt) if nxt < len(rows) else None)

    async def delete(self, collection, key):
        self.deleted.append((collection, key))
        self.data[collection] = [r for r in self.data[collection] if r["id"] != key]


class _Objects:
    def __init__(self):
        self.deleted = []

    async def delete(self, key):
        self.deleted.append(key)


def _rec(id, started, ended, locked=False, source="cam1", segs=("seg1",)):
    return {"id": id, "tenant": "acme", "source_id": source, "session_id": "s1",
            "started": started, "ended": ended, "locked": locked,
            "segments": [{"key": s} for s in segs]}


def _mk(store, objects=None):
    return StreamErasure(store=store, pii_salt=_SALT, object_store=objects)


# ── without identification ────────────────────────────────────────────────────
def test_no_camera_means_nothing_is_deleted():
    store = _Store([_rec("r1", "2026-01-01T10:00", "2026-01-01T11:00")])
    out = run(_mk(store).erase("acme", "u@example.com"))
    assert store.deleted == []
    assert out["erased"] == []


def test_it_says_it_cannot_identify_rather_than_claiming_nothing_is_here():
    # already_absent would be a LIE: the person may well be on camera.
    out = run(_mk(_Store()).erase("acme", "u@example.com"))
    assert out["already_absent"] is False
    assert out["retained"][0]["basis"] == NO_ID_BASIS
    assert out["needs_identification"] == ["source_id", "start", "end"]


def test_the_refusal_offers_a_way_forward():
    # A refusal that leaves the person stuck is not an answer.
    out = run(_mk(_Store()).erase("acme", "u@example.com"))
    why = out["retained"][0]["why"]
    assert "source_id" in why and "facial recognition" in why


# ── with identification ───────────────────────────────────────────────────────
def test_identified_footage_is_deleted_with_its_segments():
    # The row is an index; the FOOTAGE is the personal data. Deleting only the row would leave the
    # video on disk and report success.
    store = _Store([_rec("r1", "2026-01-01T10:00", "2026-01-01T11:00", segs=("a", "b"))])
    objects = _Objects()
    out = run(_mk(store, objects).erase("acme", "u@example.com", source_id="cam1"))
    assert store.deleted == [("recordings", "r1")]
    assert objects.deleted == ["a", "b"]
    assert out["erased"] == ["recordings:1"]


def test_a_locked_recording_is_never_deleted():
    # A hold that an erasure could delete is not a hold.
    store = _Store([_rec("held", "2026-01-01T10:00", "2026-01-01T11:00", locked=True)])
    out = run(_mk(store).erase("acme", "u@example.com", source_id="cam1"))
    assert store.deleted == []
    assert out["retained"] == [{"what": "recordings:1", "basis": HOLD_BASIS,
                                "why": "under legal hold — a hold an erasure could delete is not a hold"}]


def test_another_camera_is_never_touched():
    store = _Store([_rec("mine", "2026-01-01T10:00", "2026-01-01T11:00", source="cam1"),
                    _rec("other", "2026-01-01T10:00", "2026-01-01T11:00", source="cam2")])
    run(_mk(store).erase("acme", "u@example.com", source_id="cam1"))
    assert store.deleted == [("recordings", "mine")]


def test_footage_outside_the_window_survives():
    # Over-deleting destroys other people's video to satisfy one person's request.
    store = _Store([
        _rec("before", "2026-01-01T08:00", "2026-01-01T09:00"),
        _rec("inside", "2026-01-01T10:00", "2026-01-01T11:00"),
        _rec("after", "2026-01-01T20:00", "2026-01-01T21:00"),
    ])
    run(_mk(store).erase("acme", "u@example.com", source_id="cam1",
                         start="2026-01-01T09:30", end="2026-01-01T12:00"))
    assert store.deleted == [("recordings", "inside")]


def test_a_recording_straddling_the_window_edge_is_caught():
    # It contains the person during the window; ending after it does not make it someone else's.
    store = _Store([_rec("straddle", "2026-01-01T09:00", "2026-01-01T10:30")])
    run(_mk(store).erase("acme", "u@example.com", source_id="cam1",
                         start="2026-01-01T10:00", end="2026-01-01T11:00"))
    assert store.deleted == [("recordings", "straddle")]


def test_exports_of_the_footage_go_too():
    # An export is a rendered copy of the same footage; leaving it is leaving the video.
    store = _Store(exports=[{"id": "e1", "tenant": "acme", "source_id": "cam1",
                             "start": "2026-01-01T10:00", "end": "2026-01-01T11:00",
                             "file_id": "blob1"}])
    objects = _Objects()
    out = run(_mk(store, objects).erase("acme", "u@example.com", source_id="cam1"))
    assert ("exports", "e1") in store.deleted
    assert objects.deleted == ["blob1"]
    assert "exports:1" in out["erased"]


def test_erasing_twice_is_a_no_op_that_still_acks():
    store = _Store([_rec("r1", "2026-01-01T10:00", "2026-01-01T11:00")])
    first = run(_mk(store).erase("acme", "u@example.com", source_id="cam1"))
    second = run(_mk(store).erase("acme", "u@example.com", source_id="cam1"))
    assert first["erased"] == ["recordings:1"]
    assert second["erased"] == [] and second["already_absent"] is True


def test_the_ack_carries_a_pseudonym_not_the_person():
    out = run(_mk(_Store()).erase("acme", "u@example.com"))
    assert out["subject_ref"].startswith("sub_") and "u@example.com" not in out["subject_ref"]
