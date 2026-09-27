"""Round-2 fixes (stream-service). Maps to docs/audits/round2/stream-service.json."""
from __future__ import annotations

import asyncio
import json as _json

import pytest

from app.adapters.datastore.memory import MemoryDataStore
from app.adapters.stream.local_placer import LocalSessionPlacer
from app.adapters.stream.null_engine import NullStreamEngine
from app.adapters.stream.signed_playback import HmacSignedPlayback
from app.core.sealing import Sealer
from app.core.ssrf import SsrfBlocked
from app.core.stores import SessionStore, SourceStore
from app.core.stream_service import StreamConfig, StreamService


class _Clock:
    t = 1_000_000.0
    def __call__(self): return self.t


class _Interleave(MemoryDataStore):
    async def get(self, collection, key):
        await asyncio.sleep(0)
        return await super().get(collection, key)


def _cfg(**over):
    kw = dict(default_mode="on_demand", default_outputs=["hls"], max_duration_s=3600,
              idle_timeout_s=300, max_size_bytes=1_000_000, lease_seconds=30, viewer_grace_s=30,
              one_session_per_source=False, max_concurrent_global=100, max_concurrent_per_tenant=10,
              allowed_hosts=frozenset(), allow_private=True, playback_ttl=60,
              output_root="/tmp/stream-test")
    kw.update(over)
    return StreamConfig(**kw)


def _svc(ds, cfg=None, engine=None):
    engine = engine or NullStreamEngine()
    return StreamService(sources=SourceStore(ds), sessions=SessionStore(ds),
                         placer=LocalSessionPlacer(engine), engine=engine,
                         sealer=Sealer("k" * 32), signed_playback=HmacSignedPlayback("s" * 16),
                         cfg=cfg or _cfg(), clock=_Clock())


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _live(ds, key):
    row = run(ds.get("stream_quota", key))
    return int(row["live"]) if row else 0


# r2-01 — two concurrent stops of the SAME session release the reservation exactly once.
def test_concurrent_stop_releases_quota_once():
    ds = _Interleave()
    svc = _svc(ds)
    src = run(svc.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    a, _ = run(svc.start_session(tenant="t1", source_id=src.id))
    run(svc.start_session(tenant="t1", source_id=src.id))     # B, keeps a live session around
    assert _live(ds, "t::t1") == 2

    async def _race():
        await asyncio.gather(
            svc.stop_session(a.id, tenant="t1"),
            svc.stop_session(a.id, tenant="t1"),
            return_exceptions=True,
        )
    run(_race())
    assert _live(ds, "t::t1") == 1        # one release for A, not two — B's slot is intact


# r2-04 — teardown releases what was RESERVED at start, even if the cap config changed since.
def test_release_uses_reserved_not_current_config():
    ds = MemoryDataStore()
    started = _svc(ds, cfg=_cfg(max_concurrent_per_tenant=5))
    src = run(started.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    sess, _ = run(started.start_session(tenant="t1", source_id=src.id))
    assert _live(ds, "t::t1") == 1

    # operator disables the per-tenant cap; stop via a service that sees the NEW config
    toggled = _svc(ds, cfg=_cfg(max_concurrent_per_tenant=0))
    run(toggled.stop_session(sess.id, tenant="t1"))
    assert _live(ds, "t::t1") == 0        # released the tenant slot it actually took (not leaked)


# r2-05 — a session-row create failure rolls back the reservation (no leaked slot).
def test_reservation_rolled_back_when_create_fails():
    class _FailCreate(MemoryDataStore):
        # create() persists the session row via the insert-if-absent CAS (not put()), so the injected
        # failure must hook cas for the sessions collection to model a create that never lands.
        async def cas(self, collection, key, expected_version, value):
            if collection == "sessions":
                raise RuntimeError("create failed")
            return await super().cas(collection, key, expected_version, value)

    ds = _FailCreate()
    svc = _svc(ds)
    src = run(svc.register_source(tenant="t1", name="c", url="rtsp://93.184.216.34:554/s"))
    with pytest.raises(RuntimeError):
        run(svc.start_session(tenant="t1", source_id=src.id))
    assert _live(ds, "t::t1") == 0        # reservation freed despite the create failure


# r2-02 — ONVIF host is re-validated (SSRF) at connect time.
def test_pin_onvif_refuses_a_blocked_host():
    svc = _svc(MemoryDataStore(), cfg=_cfg(allow_private=False))
    with pytest.raises(SsrfBlocked):
        svc.pin_onvif({"host": "169.254.169.254", "port": 80})
    assert svc.pin_onvif({}) == {}        # nothing to pin


# stream-001 — ONVIF camera credentials are sealed at rest and NEVER returned to a client.
def test_onvif_credentials_are_sealed_and_never_public():
    ds = MemoryDataStore()
    svc = _svc(ds)
    onvif = {"host": "93.184.216.34", "port": 80, "username": "admin", "password": "s3cr3t"}
    src = run(svc.register_source(tenant="t1", name="ptz", url="rtsp://93.184.216.34:554/s",
                                  onvif=onvif))

    # (a) public_dict — what GET /sources returns — carries only connection info, no credentials.
    pub = src.public_dict()
    assert pub["onvif"] == {"host": "93.184.216.34", "port": 80}
    assert "password" not in pub["onvif"] and "username" not in pub["onvif"]
    assert "s3cr3t" not in _json.dumps(pub) and "admin" not in _json.dumps(pub)

    # (b) at rest — the stored row holds the secrets ONLY inside the sealed blob, never in plaintext.
    row = run(ds.get("sources", src.id))
    assert "s3cr3t" not in _json.dumps({k: v for k, v in row.items() if k != "onvif_sealed"})
    assert row["onvif_sealed"] and "s3cr3t" not in row["onvif_sealed"]
    assert row["onvif"] == {"host": "93.184.216.34", "port": 80}

    # (c) the service can still reassemble the full credentials for a PTZ call.
    full = svc.unseal_onvif(src)
    assert full["username"] == "admin" and full["password"] == "s3cr3t" and full["host"] == "93.184.216.34"


def test_onvif_sealed_blob_is_aad_bound_to_its_source():
    # A tampered row that relocates one source's sealed onvif blob onto another id must not decrypt,
    # so credentials cannot be moved between rows under the shared SEAL_KEY.
    from dataclasses import replace
    ds = MemoryDataStore()
    svc = _svc(ds)
    src = run(svc.register_source(tenant="t1", name="ptz", url="rtsp://93.184.216.34:554/s",
                                  onvif={"host": "93.184.216.34", "username": "a", "password": "p"}))
    moved = replace(src, id="other-source-id")   # same sealed blob, different row id
    assert svc.unseal_onvif(moved) == {"host": "93.184.216.34"}   # secrets refused, only public kept


def test_public_onvif_strips_secrets_from_a_legacy_plaintext_row():
    # A row written by an older build still has plaintext secrets inside `onvif` and no sealed blob.
    # public_dict must strip them defensively; unseal_onvif must still surface them for PTZ.
    from app.core.models import Source
    legacy = Source.from_dict({"id": "s1", "tenant": "t1", "name": "old",
                               "onvif": {"host": "10.0.0.9", "username": "u", "password": "pw"}})
    assert legacy.public_dict()["onvif"] == {"host": "10.0.0.9"}
    svc = _svc(MemoryDataStore())
    assert svc.unseal_onvif(legacy) == {"host": "10.0.0.9", "username": "u", "password": "pw"}


# cas-create — SessionStore.create must CREATE via the insert-if-absent CAS, so the LIVE->terminal
# cas_save() in _stop wins on the strict postgres adapter. A put-created (versionless) row made that
# first cas a no-op on postgres: the session never reached a terminal state and its concurrency slot
# leaked, while the lax memory cas kept tests green. Asserting the stamped version catches it.
def test_session_create_version_stamped_and_cas_save_wins():
    from app.core.models import STARTING, Session
    st = MemoryDataStore()
    sessions = SessionStore(st)
    sess = Session(id="sess_1", tenant="t1", source_id="src_1", status=STARTING)
    run(sessions.create(sess))
    assert int(run(st.get("sessions", "sess_1")).get("version", 0)) == 1   # cas-created (put would be 0)
    assert sess.version == 1
    assert run(sessions.cas_save(sess, sess.version)) is True              # version-pinned update wins
