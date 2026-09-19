"""StreamService — the core. Register sources, start/stop sessions with modes + guards + quota,
heartbeat, and enforce the guarantee that a session can never run forever.

The bytes never touch this service: it drives a `SessionPlacer` (which owns the engine process) and
hands back signed playback URLs. All time comparisons go through an injected `clock()` (epoch
seconds) so guards are deterministically testable.
"""
from __future__ import annotations

import time
from typing import Any, Callable

from app.core.models import (
    ERROR, LIVE, STOPPED, Guards, Session, Source, new_id, now,
)
from app.core.sealing import Sealer, redact_url, source_aad
from app.core.ssrf import SsrfBlocked, validate_source_url
from app.core.stores import SessionStore, SourceStore
from app.ports.stream_types import EngineSpec


class SourceNotFound(Exception):
    pass


class QuotaExceeded(Exception):
    def __init__(self, dimension: str) -> None:
        super().__init__(dimension)
        self.dimension = dimension


class StreamConfig:
    """The slice of settings the core needs — passed in so the core stays free of pydantic."""

    def __init__(self, *, default_mode: str, default_outputs: list[str],
                 max_duration_s: int, idle_timeout_s: int, max_size_bytes: int,
                 lease_seconds: int, viewer_grace_s: int, one_session_per_source: bool,
                 max_concurrent_global: int, max_concurrent_per_tenant: int,
                 allowed_hosts, allow_private: bool, playback_ttl: int,
                 output_root: str, webrtc_enabled: bool = False, webrtc_media_base: str = "",
                 hls_segment_seconds: int = 2, hls_window_segments: int = 5,
                 record_segment_seconds: int = 60, record_format: str = "fmp4",
                 connect_timeout_s: int = 20, reconnect_max: int = 20,
                 reconnect_backoff_ms: int = 500, webrtc_max_viewers: int = 25) -> None:
        self.webrtc_enabled = webrtc_enabled
        self.webrtc_media_base = webrtc_media_base.rstrip("/")
        self.hls_segment_seconds = hls_segment_seconds
        self.hls_window_segments = hls_window_segments
        self.record_segment_seconds = record_segment_seconds
        self.record_format = record_format
        self.connect_timeout_s = connect_timeout_s
        self.reconnect_max = reconnect_max
        self.reconnect_backoff_ms = reconnect_backoff_ms
        self.webrtc_max_viewers = webrtc_max_viewers
        self.default_mode = default_mode
        self.default_outputs = default_outputs
        self.max_duration_s = max_duration_s
        self.idle_timeout_s = idle_timeout_s
        self.max_size_bytes = max_size_bytes
        self.lease_seconds = lease_seconds
        self.viewer_grace_s = viewer_grace_s
        self.one_session_per_source = one_session_per_source
        self.max_concurrent_global = max_concurrent_global
        self.max_concurrent_per_tenant = max_concurrent_per_tenant
        self.allowed_hosts = allowed_hosts
        self.allow_private = allow_private
        self.playback_ttl = playback_ttl
        self.output_root = output_root


class StreamService:
    def __init__(self, *, sources: SourceStore, sessions: SessionStore, placer, engine,
                 sealer: Sealer, signed_playback, cfg: StreamConfig,
                 clock: Callable[[], float] = time.time, events=None,
                 shipper=None, record_ext: str = "mp4", media_egress=None) -> None:
        self._egress = media_egress
        self._sources = sources
        self._sessions = sessions
        self._placer = placer
        self._engine = engine
        self._sealer = sealer
        self._signed = signed_playback
        self._cfg = cfg
        self._clock = clock
        self._events = events
        self._shipper = shipper
        self._record_ext = record_ext

    # ── sources ────────────────────────────────────────────────────────────────
    async def register_source(self, *, tenant: str, name: str, url: str, transport: str = "tcp",
                              kind: str = "rtsp", onvif: dict | None = None,
                              tags: list | None = None, group_id: str = "",
                              default_profile: str = "default") -> Source:
        self._check_ssrf(url)                       # validate BEFORE we seal/store
        if onvif and onvif.get("host"):
            # The ONVIF/PTZ host is also connected to (with credentials) — validate it too, or a
            # caller registers a source whose PTZ control points at an internal/metadata host.
            port = onvif.get("port") or 80
            self._check_ssrf(f"http://{onvif['host']}:{int(port)}")
        # Compute the id first so the sealed URL can be bound to it (AAD): the blob is then tied to
        # this exact source row and cannot be relocated onto another under the shared SEAL_KEY.
        sid = new_id()
        src = Source(id=sid, tenant=tenant, name=name,
                     url_sealed=self._sealer.seal(url, aad=source_aad(sid)), url_host=redact_url(url),
                     transport=transport, kind=kind, onvif=onvif or {}, tags=tags or [],
                     group_id=group_id, default_profile=default_profile)
        return await self._sources.create(src)

    async def get_source(self, source_id: str, *, tenant: str) -> Source:
        src = await self._sources.get(source_id)
        if src is None or src.tenant != tenant:
            raise SourceNotFound(source_id)         # hidden across tenants (404, not 403)
        return src

    async def probe_source(self, source_id: str, *, tenant: str):
        src = await self.get_source(source_id, tenant=tenant)
        url = self._sealer.unseal(src.url_sealed, aad=source_aad(src.id))
        # Probe the PINNED ip, not the hostname: validating the host then connecting by name reopens a
        # DNS-rebinding window (the name can resolve to a blocked IP between the check and the connect),
        # exactly what _check_ssrf's own contract forbids. Mirrors start_session (stream-r2-03).
        resolved = self._check_ssrf(url)
        return await self._engine.probe(self._pin_url(url, resolved.ip), transport=src.transport)

    # ── sessions ────────────────────────────────────────────────────────────────
    async def start_session(self, *, tenant: str, source_id: str, mode: str | None = None,
                            outputs: list[str] | None = None, profile: str | None = None,
                            guards: dict | None = None) -> tuple[Session, dict[str, Any]]:
        src = await self.get_source(source_id, tenant=tenant)
        if not src.enabled:
            raise SourceNotFound(source_id)         # a disabled source refuses new sessions

        url = self._sealer.unseal(src.url_sealed, aad=source_aad(src.id))
        resolved = self._check_ssrf(url)            # re-validate at connect time
        pinned_url = self._pin_url(url, resolved.ip)  # connect to the validated IP — rebinding-safe

        sess = Session(
            id=new_id(), tenant=tenant, source_id=source_id,
            mode=mode or self._cfg.default_mode,
            outputs=outputs or list(self._cfg.default_outputs),
            profile=profile or src.default_profile,
            guards=self._resolve_guards(guards), status=LIVE, started_at=self._now_iso())

        # ATOMIC quota reservation. The old count-then-create was a TOCTOU: K concurrent starts each
        # read count<cap before any create committed, so all passed and the cap was breached. Now each
        # cap is a compare-and-set counter (per-source is a claim), incremented BEFORE the session is
        # created; only `cap` concurrent increments can win, so the cap holds. Reservations are released
        # when the session leaves LIVE, and a counter that drifts high (a crashed session that never
        # released) self-heals against the real live count on the next reservation.
        reserved = await self._reserve_quota(sess)   # raises QuotaExceeded before anything is created
        # Record exactly what was reserved ON the session, so teardown releases this set even if the
        # cap config is toggled meanwhile (stream-r2-04).
        sess.reserved = dict(reserved)
        placement = None
        try:
            whip = ""
            if "webrtc" in sess.outputs and self._cfg.webrtc_enabled and self._cfg.webrtc_media_base:
                whip = f"{self._cfg.webrtc_media_base}/{sess.id}/whip"
            spec = EngineSpec(source_url=pinned_url, transport=src.transport, outputs=tuple(sess.outputs),
                              output_dir=f"{self._cfg.output_root}/{sess.id}", profile=src.default_profile,
                              webrtc_whip_url=whip, hls_segment_seconds=self._cfg.hls_segment_seconds,
                              hls_window_segments=self._cfg.hls_window_segments,
                              record_segment_seconds=self._cfg.record_segment_seconds,
                              record_format=self._cfg.record_format)
            placement = await self._placer.start(sess.id, spec)
            sess.placement = {"placer": placement.placer, "worker_id": placement.worker_id,
                              "handle_id": placement.handle.id, "output_dir": spec.output_dir}
            sess.lease_expires_at = self._lease_deadline()
            # create() is INSIDE the rollback: if the session row fails to persist, the reservation
            # (and the started placement) would otherwise leak — a slot held by a session that does
            # not exist (stream-r2-05).
            await self._sessions.create(sess)
        except BaseException:
            if placement is not None:
                try:
                    await self._placer.stop(placement)
                except Exception:   # noqa: BLE001 — best-effort teardown; the reservation MUST be freed
                    pass
            await self._release_quota(reserved, sess)
            raise
        await self._emit("stream.session.started", sess)
        return sess, self._playback(sess)

    async def get_session(self, session_id: str, *, tenant: str) -> Session:
        sess = await self._sessions.get(session_id)
        if sess is None or sess.tenant != tenant:
            raise SourceNotFound(session_id)
        return sess

    async def stop_session(self, session_id: str, *, tenant: str, reason: str = "user") -> Session:
        sess = await self.get_session(session_id, tenant=tenant)
        return await self._stop(sess, reason)

    async def playback(self, session_id: str, *, tenant: str) -> dict[str, Any]:
        sess = await self.get_session(session_id, tenant=tenant)
        self._note_viewer(sess)
        await self._sessions.save(sess)
        return self._playback(sess)

    async def heartbeat(self, session_id: str, stats: dict | None = None) -> Session | None:
        """Engine→service: extend the lease and record stats. The size guard is checked here too."""
        sess = await self._sessions.get(session_id)
        if sess is None or not sess.is_live:
            return sess
        if stats:
            sess.stats = stats
        sess.lease_expires_at = self._lease_deadline()
        # Size guard: a recording that hit the cap stops now.
        g = sess.guards
        if g.max_size_bytes and int(sess.stats.get("bytes_out", 0)) >= g.max_size_bytes:
            return await self._stop(sess, "max_size")
        return await self._sessions.save(sess)

    # ── guard enforcement + reconcile (sweeper/CronJob) ─────────────────────────
    async def enforce_and_reconcile(self, *, limit: int = 200) -> dict[str, int]:
        """Walk live sessions: stop any past a guard; mark dead any whose engine is gone."""
        stopped = dead = 0
        cursor = None
        seen: set[str] = set()
        while True:
            batch, cursor = await self._sessions.iter_live(limit=limit, after=cursor)
            for sess in batch:
                seen.add(sess.id)
                reason = self._guard_breached(sess)
                if reason:
                    await self._stop(sess, reason)
                    stopped += 1
            if not cursor:
                break
        res = await self._placer.reconcile(seen)
        for sid in res.dead_sessions:
            sess = await self._sessions.get(sid)
            if sess and sess.is_live:
                # Route through _stop so the concurrency reservation is released (was a bare status
                # write that left the tenant's counter permanently consuming a slot for a dead session).
                await self._stop(sess, "error")
                dead += 1
        return {"stopped": stopped, "reaped_dead": dead,
                "killed_orphans": len(res.killed_orphans)}

    async def enforce_retention(self, *, days: int, recordings, sink, limit: int = 200) -> dict[str, int]:
        """Delete recordings older than `days`, releasing each segment's bytes in the sink.

        A LOCKED recording (legal hold) is never touched — the store's iter_expired already filters
        them. Bounded, off-request. Idempotent: a deleted recording just isn't found next time.
        """
        import datetime as _dt
        cutoff = (_dt.datetime.fromtimestamp(self._clock(), _dt.timezone.utc)
                  - _dt.timedelta(days=days)).isoformat()
        deleted = segments = 0
        cursor = None
        while True:
            batch, cursor = await recordings.iter_expired(cutoff, limit=limit, after=cursor)
            for rec in batch:
                for seg in rec.segments:
                    try:
                        await sink.delete(seg.get("ref", ""))
                        segments += 1
                    except Exception:   # noqa: BLE001
                        pass
                await recordings.delete(rec.id)
                deleted += 1
            if not cursor:
                break
        return {"deleted_recordings": deleted, "deleted_segments": segments}

    # ── helpers ──────────────────────────────────────────────────────────────────
    def _guard_breached(self, sess: Session) -> str:
        g = sess.guards
        started = _epoch(sess.started_at)
        elapsed = self._clock() - started if started else 0
        if g.max_duration_s and elapsed >= g.max_duration_s:
            return "max_duration"
        if g.idle_timeout_s and sess.mode in ("on_demand", "viewer_driven"):
            last = _epoch(sess.last_viewer_at) or started
            if last and (self._clock() - last) >= g.idle_timeout_s:
                return "idle"
        if _epoch(sess.lease_expires_at) and self._clock() > _epoch(sess.lease_expires_at):
            return "error"       # heartbeat missed → zombie; reconcile confirms, this is the backstop
        return ""

    async def poll_motion(self, detector, triggers, *, debounce_s: int) -> dict[str, int]:
        """Poll motion for live sessions and fire triggers (debounced) as EventBus events.

        Loose-coupled: a motion event is published as `stream.motion.detected` with the source's
        declared actions; webhook-service / notification-service consume it. Debounce prevents a
        scene of continuous movement from flooding downstream.
        """
        from app.ports.stream_types import EngineHandle
        fired = 0
        cursor = None
        while True:
            batch, cursor = await self._sessions.iter_live(limit=200, after=cursor)
            for sess in batch:
                odir = sess.placement.get("output_dir")
                if not odir:
                    continue
                handle = EngineHandle(id=sess.placement.get("handle_id", ""), output_dir=odir)
                events = await detector.poll(sess.id, handle)
                if not events:
                    continue
                last = _epoch(sess.stats.get("last_motion_at", ""))
                if last and (self._clock() - last) < debounce_s:
                    continue                             # debounced
                sess.stats["last_motion_at"] = self._now_iso()
                await self._sessions.save(sess)
                trigs = await triggers.get(sess.source_id)
                actions = sorted({a for t in trigs if t.get("on") == "motion" for a in t.get("do", [])})
                await self._publish("stream.motion.detected", {
                    "session_id": sess.id, "tenant": sess.tenant, "source_id": sess.source_id,
                    "score": events[0].score, "actions": actions})
                fired += 1
            if not cursor:
                break
        return {"motion": fired}

    async def ship_live_recordings(self) -> dict[str, int]:
        """Ship completed recording segments for every live recording session (sweep-driven)."""
        if self._shipper is None:
            return {"shipped": 0}
        shipped = 0
        cursor = None
        while True:
            batch, cursor = await self._sessions.iter_live(limit=200, after=cursor)
            for sess in batch:
                if "record" in sess.outputs and sess.placement.get("output_dir"):
                    shipped += await self._shipper.ship_session(
                        tenant=sess.tenant, session_id=sess.id, source_id=sess.source_id,
                        output_dir=sess.placement["output_dir"], record_ext=self._record_ext,
                        final=False)
            if not cursor:
                break
        return {"shipped": shipped}

    async def _stop(self, sess: Session, reason: str) -> Session:
        if sess.is_terminal:
            return sess
        # ATOMIC claim of the LIVE->terminal transition. The old `if is_terminal` guard was a
        # non-atomic read-modify-write: two concurrent stops (a user stop + a reaper, say) both saw
        # LIVE and both released the reservation, so the concurrency counter under-counted and the cap
        # could later be breached. CAS the session status; only the caller that WINS releases the
        # quota. A loser reloads and returns without touching the counter (stream-r2-01).
        expected = int(sess.version)
        sess.status = STOPPED if reason != "error" else ERROR
        sess.reason = reason
        if not await self._sessions.cas_save(sess, expected):
            return await self._sessions.get(sess.id) or sess

        # Ship the final recording segment(s) before tearing down — ffmpeg has closed them now.
        if self._shipper is not None and "record" in sess.outputs and sess.placement.get("output_dir"):
            try:
                await self._shipper.ship_session(
                    tenant=sess.tenant, session_id=sess.id, source_id=sess.source_id,
                    output_dir=sess.placement["output_dir"], record_ext=self._record_ext, final=True)
            except Exception:   # noqa: BLE001 — shipping must never block a stop
                pass
        placement = sess.placement
        try:
            if placement:
                from app.ports.stream_types import EngineHandle, Placement
                p = Placement(placer=placement.get("placer", "local"),
                              worker_id=placement.get("worker_id", "local"),
                              handle=EngineHandle(id=placement.get("handle_id", "")))
                await self._placer.stop(p)
        except Exception:   # noqa: BLE001 — a process already gone is fine; we still mark stopped
            pass
        # Release EXACTLY what this session reserved at start (stream-r2-04) — not a set recomputed
        # from the CURRENT config, which a cap toggle would make leak or steal slots. Fall back to the
        # config-derived map only for legacy sessions created before `reserved` was persisted.
        await self._release_quota(sess.reserved or self._release_map(), sess)
        await self._emit("stream.session.stopped", sess)
        return sess

    def _note_viewer(self, sess: Session) -> None:
        sess.last_viewer_at = self._now_iso()
        sess.viewers = max(1, sess.viewers)

    def _now_iso(self) -> str:
        # Derived from the injected clock so guard math is consistent and deterministically testable.
        import datetime as _dt
        return _dt.datetime.fromtimestamp(self._clock(), _dt.timezone.utc).isoformat()

    def _resolve_guards(self, override: dict | None) -> Guards:
        g = Guards.from_dict(override or {})
        # Fill unset from config defaults, then CLAMP to the service maxima — a tenant can never ask
        # for an unbounded stream.
        g.max_duration_s = _clamp(g.max_duration_s or self._cfg.max_duration_s, self._cfg.max_duration_s)
        g.idle_timeout_s = g.idle_timeout_s or self._cfg.idle_timeout_s
        g.max_size_bytes = _clamp(g.max_size_bytes or self._cfg.max_size_bytes, self._cfg.max_size_bytes)
        return g

    # ── atomic concurrency reservation (cas counters + per-source claim) ──────────
    _QUOTA = "stream_quota"

    async def _reserve_quota(self, sess: Session) -> dict[str, bool]:
        """Reserve the concurrency slots this session needs, atomically, BEFORE it is created. Raises
        QuotaExceeded (having rolled back anything already taken) if a cap is full. Returns which slots
        were taken, so the caller can release exactly those if a later step fails."""
        taken: dict[str, bool] = {}
        try:
            cap = self._cfg.max_concurrent_per_tenant
            if cap and not await self._incr(f"t::{sess.tenant}", cap):
                raise QuotaExceeded("concurrency")
            taken["tenant"] = bool(cap)
            gcap = self._cfg.max_concurrent_global
            if gcap and not await self._incr("__global__", gcap):
                raise QuotaExceeded("concurrency_global")
            taken["global"] = bool(gcap)
            if self._cfg.one_session_per_source:
                if not await self._claim_source(sess.tenant, sess.source_id):
                    raise QuotaExceeded("one_session_per_source")
                taken["source"] = True
            return taken
        except QuotaExceeded:
            await self._release_quota(taken, sess)      # undo whatever we already took
            raise

    async def _release_quota(self, taken: dict[str, bool], sess: Session) -> None:
        if taken.get("tenant"):
            await self._decr(f"t::{sess.tenant}")
        if taken.get("global"):
            await self._decr("__global__")
        if taken.get("source"):
            await self._sessions.store.delete(self._QUOTA, f"src::{sess.tenant}::{sess.source_id}")

    def _release_map(self) -> dict[str, bool]:
        """What a live session holds, per config — used to release on teardown without per-session state."""
        return {"tenant": bool(self._cfg.max_concurrent_per_tenant),
                "global": bool(self._cfg.max_concurrent_global),
                "source": bool(self._cfg.one_session_per_source)}

    async def _incr(self, key: str, cap: int) -> bool:
        """Atomically bump a live-count counter iff below cap. Pure compare-and-set: concurrent
        increments serialise on the version, so at most `cap` can ever win — the cap holds no matter
        how many starts race. The counter is a RESERVATION count (bumped before the session row is
        created), so it is NOT reconciled here against live session rows — during a burst those rows do
        not exist yet, and a reconcile would wrongly reset the count and let the cap be breached. Every
        reservation is released on the session's LIVE->non-LIVE transition (incl. reaper-detected
        death via _stop), so drift is bounded to genuinely in-flight work."""
        for _ in range(128):
            row = await self._sessions.store.get(self._QUOTA, key)
            live = int(row.get("live", 0)) if row else 0
            ver = int(row.get("version", 0)) if row else 0
            if live >= cap:
                return False
            if await self._sessions.store.cas(self._QUOTA, key, ver, {"key": key, "live": live + 1}):
                return True
        return False

    async def _decr(self, key: str) -> None:
        for _ in range(128):
            row = await self._sessions.store.get(self._QUOTA, key)
            if not row:
                return
            ver = int(row.get("version", 0))
            live = max(0, int(row.get("live", 0)) - 1)
            if await self._sessions.store.cas(self._QUOTA, key, ver, {"key": key, "live": live}):
                return

    async def _claim_source(self, tenant: str, source_id: str) -> bool:
        """Claim the single live slot for a source (atomic insert-if-absent). The claim IS the
        reservation and is released on the holder's LIVE->non-LIVE transition (incl. reaper death),
        so a crashed session's claim is freed when its session is reaped — no live-count reconcile
        here, which during a concurrent claim would see zero live sessions (created later) and hand the
        camera to two starts at once."""
        key = f"src::{tenant}::{source_id}"
        for _ in range(8):
            if await self._sessions.store.cas(self._QUOTA, key, 0, {"key": key}):
                return True                         # won the insert
            if await self._sessions.store.get(self._QUOTA, key) is not None:
                return False                        # a claim already exists — someone else holds it
        return False

    def _check_ssrf(self, url: str):
        """Validate + resolve, returning the Resolved (with the pinned IP). Callers that connect
        (start_session) MUST use the pinned IP, or ffmpeg re-resolves the hostname and a DNS-rebinding
        attacker swaps in an internal IP after validation."""
        try:
            return validate_source_url(url, allowed_hosts=self._cfg.allowed_hosts,
                                       allow_private=self._cfg.allow_private)
        except SsrfBlocked as exc:
            raise SsrfBlocked(str(exc)) from exc

    def pin_onvif(self, onvif: dict) -> dict:
        """Re-validate the ONVIF host against the SSRF policy and PIN it to the resolved IP at every
        PTZ/capability call. The stored host was validated at registration, but connecting by name
        later reopens a DNS-rebinding window (the name can now resolve to an internal IP) — and the
        connection carries the camera credentials. Returns a copy of `onvif` with host set to the
        validated IP (stream-r2-02)."""
        onvif = dict(onvif or {})
        host = onvif.get("host")
        if not host:
            return onvif
        port = int(onvif.get("port") or 80)
        resolved = self._check_ssrf(f"http://{host}:{port}")
        onvif["host"] = resolved.ip
        return onvif

    @staticmethod
    def _pin_url(url: str, ip: str) -> str:
        """Rewrite the URL's host to the validated IP so the connection cannot be rebound. IPv6 is
        bracketed; userinfo/port/path/query are preserved."""
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        host_lit = f"[{ip}]" if ":" in ip else ip
        userinfo = ""
        if parts.username:
            userinfo = parts.username + (f":{parts.password}" if parts.password else "") + "@"
        netloc = f"{userinfo}{host_lit}" + (f":{parts.port}" if parts.port else "")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))

    def _lease_deadline(self) -> str:
        import datetime as _dt
        return (_dt.datetime.fromtimestamp(self._clock(), _dt.timezone.utc)
                + _dt.timedelta(seconds=self._cfg.lease_seconds)).isoformat()

    def _playback(self, sess: Session) -> dict[str, Any]:
        ttl = self._cfg.playback_ttl
        out: dict[str, Any] = {"expires_at": self._token_exp(ttl)}
        if "hls" in sess.outputs:
            tok = self._signed.sign(tenant=sess.tenant, session_id=sess.id, output="hls", ttl=ttl)
            out["hls"] = {"playlist_url": f"/media/hls/{sess.id}/index.m3u8?token={tok}"}
        if "webrtc" in sess.outputs and self._cfg.webrtc_enabled and self._cfg.webrtc_media_base:
            # The browser subscribes to the media server's WHEP endpoint directly (media not through
            # the app), with ICE servers (TURN) from the egress adapter.
            ice = self._egress.ice_servers() if self._egress is not None else []
            out["webrtc"] = {"whep_url": f"{self._cfg.webrtc_media_base}/{sess.id}/whep",
                             "ice_servers": ice}
        if "snapshot" in sess.outputs or "hls" in sess.outputs:
            tok = self._signed.sign(tenant=sess.tenant, session_id=sess.id, output="snapshot", ttl=ttl)
            out["snapshot_url"] = f"/media/snapshot/{sess.id}?token={tok}"
        return out

    def _token_exp(self, ttl: int) -> str:
        import datetime as _dt
        return (_dt.datetime.now(_dt.timezone.utc) + _dt.timedelta(seconds=ttl)).isoformat()

    async def _emit(self, topic: str, sess: Session) -> None:
        await self._publish(topic, {"session_id": sess.id, "tenant": sess.tenant,
                                    "source_id": sess.source_id, "status": sess.status,
                                    "reason": sess.reason})

    async def _publish(self, topic: str, payload: dict) -> None:
        if self._events is not None:
            try:
                await self._events.publish(topic, payload)
            except Exception:   # noqa: BLE001 — telemetry must never break a stream
                pass


def _clamp(value: int, ceiling: int) -> int:
    if ceiling and value > ceiling:
        return ceiling
    return value


def _epoch(iso: str) -> float:
    if not iso:
        return 0.0
    try:
        import datetime as _dt
        return _dt.datetime.fromisoformat(iso).timestamp()
    except Exception:   # noqa: BLE001
        return 0.0
