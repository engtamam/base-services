"""Non-security MEDIUM remediation (stream-service):
  011  retention iter_expired no longer keys on locked==False, so a legacy row missing the field is swept.
  013  the per-poll viewer mark is debounced (no session-row write on every playback poll).
  016  operator audit actor is never empty even when PII_SALT is unset.
"""
from __future__ import annotations

import asyncio

from app.adapters.datastore.memory import MemoryDataStore
from app.core.stores import RecordingStore, _RECORDINGS


def run(c):
    return asyncio.run(c)


# ── 011: legacy row missing `locked` is still retention-swept ──────────────────
def test_iter_expired_sweeps_a_row_missing_the_locked_field():
    st = MemoryDataStore()
    # a legacy recording with NO `locked` key, older than the cutoff
    run(st.put(_RECORDINGS, "old", {"id": "old", "tenant": "t", "source_id": "s", "session_id": "x",
                                    "started": "2020-01-01T00:00:00Z", "trigger": "continuous"}))
    # a locked (legal-hold) old row must NOT be swept
    run(st.put(_RECORDINGS, "held", {"id": "held", "tenant": "t", "source_id": "s", "session_id": "x",
                                     "started": "2020-01-01T00:00:00Z", "trigger": "manual",
                                     "locked": True}))
    expired, _ = run(RecordingStore(st).iter_expired("2025-01-01T00:00:00Z"))
    ids = {r.id for r in expired}
    assert "old" in ids          # legacy row (no locked field) IS swept
    assert "held" not in ids     # legal hold is respected


# ── 013: playback viewer mark is debounced ────────────────────────────────────
def test_note_viewer_debounces_repeated_polls():
    from app.core.stream_service import StreamConfig, StreamService
    from app.core.models import Session

    clock = {"t": 1000.0}
    cfg = StreamConfig(default_mode="viewer_driven", default_outputs=["hls"], max_duration_s=0,
                       idle_timeout_s=0, max_size_bytes=0, lease_seconds=60, viewer_grace_s=0,
                       one_session_per_source=False, max_concurrent_global=0,
                       max_concurrent_per_tenant=0, allowed_hosts=[], allow_private=True,
                       playback_ttl=60, output_root="/tmp", viewer_mark_debounce_s=10)
    svc = StreamService.__new__(StreamService)
    svc._cfg = cfg
    svc._clock = lambda: clock["t"]   # _now_iso derives from this (real method), so marks are epoch-parseable

    sess = Session(id="s1", tenant="t", source_id="src", mode="viewer_driven")
    assert svc._note_viewer(sess) is True            # first mark: write
    assert svc._note_viewer(sess) is False           # within debounce: skip the write
    clock["t"] += 11
    assert svc._note_viewer(sess) is True             # past debounce: write again


# ── 016: operator audit actor is never empty without a salt ────────────────────
def test_actor_is_non_empty_without_a_salt(monkeypatch):
    import app.api.v1.operator as op

    class _S:
        PII_SALT = ""
        INTERNAL_SERVICE_TOKEN = "x" * 40

    monkeypatch.setattr(op, "get_settings", lambda: _S())

    class _P:
        tenant = "t"
        subject = "u@example.com"

    actor = op._actor(_P())
    assert actor and actor.startswith("op_unsalted_")
    assert "u@example.com" not in actor               # not the raw identifier
