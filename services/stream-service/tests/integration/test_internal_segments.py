"""/internal/stream/segments — the file_ids a service with identity needs to link footage.

Stream stores by camera+time and cannot know who is in a frame. So there are two ways a person's
footage gets erased: stream's own erase() (camera+window → delete), and this route, which hands the
segment file_ids to the service that DOES have identity (squad) so it can attach the subject in
file-service. This tests the second path.
"""
from __future__ import annotations

import asyncio

import pytest

from tests.conftest import INTERNAL_TOKEN

from app.core.models import Recording
from app.core.stores import RecordingStore


def _seed(client, rec: Recording):
    store = RecordingStore(client.app.state.container.datastore)
    asyncio.run(store.create(rec))


def _rec(rid, source, started, ended, segs, locked=False, tenant="acme"):
    return Recording(id=rid, tenant=tenant, source_id=source, session_id="s1",
                     started=started, ended=ended, locked=locked,
                     segments=[{"ref": r, "start": started, "end": ended} for r in segs])


def test_it_requires_the_internal_token(client):
    assert client.get("/internal/stream/segments?tenant=acme&source_id=cam1").status_code == 401


def test_it_returns_segment_file_ids_for_a_camera(client):
    _seed(client, _rec("rc1", "cam-seg-a", "2026-02-01T10:00", "2026-02-01T11:00", ["f1", "f2"]))
    r = client.get("/internal/stream/segments?tenant=acme&source_id=cam-seg-a",
                   headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert r.status_code == 200
    ids = [s["file_id"] for s in r.json()["segments"]]
    assert ids == ["f1", "f2"]
    assert r.json()["segments"][0]["recording_id"] == "rc1"


def test_source_id_is_required_so_a_call_cannot_dump_every_tenant_segment(client):
    # A missing source_id is a 422 from the query validator, not an unscoped list.
    r = client.get("/internal/stream/segments?tenant=acme",
                   headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert r.status_code == 422


def test_a_window_narrows_to_overlapping_recordings(client):
    _seed(client, _rec("in", "cam-seg-w", "2026-03-01T10:00", "2026-03-01T11:00", ["hit"]))
    _seed(client, _rec("out", "cam-seg-w", "2026-03-01T20:00", "2026-03-01T21:00", ["miss"]))
    r = client.get("/internal/stream/segments?tenant=acme&source_id=cam-seg-w"
                   "&start=2026-03-01T09:30&end=2026-03-01T12:00",
                   headers={"X-Internal-Token": INTERNAL_TOKEN})
    ids = [s["file_id"] for s in r.json()["segments"]]
    assert ids == ["hit"]


def test_a_locked_recording_is_still_listed(client):
    # A hold governs DELETION, not who may be linked — the subject still has a right to know.
    _seed(client, _rec("held", "cam-seg-l", "2026-04-01T10:00", "2026-04-01T11:00", ["lf"], locked=True))
    r = client.get("/internal/stream/segments?tenant=acme&source_id=cam-seg-l",
                   headers={"X-Internal-Token": INTERNAL_TOKEN})
    segs = r.json()["segments"]
    assert [s["file_id"] for s in segs] == ["lf"]
    assert segs[0]["locked"] is True


def test_another_tenant_is_not_returned(client):
    _seed(client, _rec("mine", "cam-seg-t", "2026-05-01T10:00", "2026-05-01T11:00", ["ours"]))
    _seed(client, _rec("theirs", "cam-seg-t", "2026-05-01T10:00", "2026-05-01T11:00", ["notours"],
                       tenant="globex"))
    r = client.get("/internal/stream/segments?tenant=acme&source_id=cam-seg-t",
                   headers={"X-Internal-Token": INTERNAL_TOKEN})
    assert [s["file_id"] for s in r.json()["segments"]] == ["ours"]
