# Phase 35 — stream-service (live camera / RTSP ingest, control, record, view)

> **Target release: v0.6.0.** A new Tier-2 service. It ingests a live source (RTSP/ONVIF camera, or
> any URL an engine can open), and turns it into the things a user actually needs: a **live view** in
> a browser (HLS + WebRTC), **snapshots/thumbnails**, **recordings** (stored via file-service), motion
> **events**, and **PTZ** control — all tenant-scoped, capability-gated, and config-driven so it fits a
> one-camera SMB and a thousand-camera operator with the same code.
>
> The hard truth that shapes everything: **a live stream is a long-lived, stateful process**, unlike
> every stateless request-service before it. So placement is abstracted behind a `SessionPlacer` port
> (local now, worker-pool later), the media bytes NEVER traverse the API pod, and a session can never
> outlive its guards — because the #1 real-world failure is a forgotten stream burning money.

Self-contained (own `.venv`, no shared library). Reuses **file-service** for recording storage,
**webhook-service** + **notification-service** for alerts, and the **EventBus** for lifecycle events —
by HTTP/events, never by import.

---

## 0. Definition of Done

### (1) Is this on the hot path? — YES, and the lever is "keep the app out of the media plane"

Two planes, treated oppositely:
- **Control plane** (start/stop/snapshot/PTZ/list): ordinary request traffic, low QPS, cache the reads
  (source list, session status) with generation-keyed invalidation. O(1) per request.
- **Media plane** (the actual video bytes): must **never** flow through the API pod. The stream engine
  writes HLS segments and recordings straight to object storage / file-service, and serves live via
  **signed segment URLs** (HLS) or a **WebRTC egress** the engine owns. The API only ever hands back a
  *URL*, never a frame. **Benchmark:** starting/among/stopping a session and serving its playlist does
  bounded, size-independent app work; assert zero video bytes are read by the API process.

### (2) Does every config knob have a consumer?

Every knob in §Config maps to a consumer (the knob-guard gate enforces it): engine/ptz/motion/placer
drivers, the auto-stop guards, transcode profiles, retention, quotas, SSRF allowlist, playback TTL.

### (3) What production needs that "open the RTSP URL" omits

- **The forgot-to-stop problem is first-class** — every session has a hard TTL, idle auto-stop,
  size cap, optional schedule, and a per-tenant quota; a sweeper reaps anything orphaned or over-limit.
- **Reconnect with backoff + circuit breaker** — cameras drop constantly; a session survives a blip
  and a dead camera doesn't hammer the network or spin a hot loop.
- **Credential sealing + redaction** — an RTSP URL carries `user:pass@host`; it is sealed at rest and
  scrubbed from logs, manifests, events, and HLS playlists.
- **SSRF guard** — the source URL is attacker-influenced; it is validated against a config allowlist and
  blocked from internal/metadata ranges, re-checked on every (re)connect (DNS-rebinding safe).
- **Signed, short-lived playback** — a live view is not public; HLS/WebRTC access is a scoped token.
- **Orphan-process reaping** — a session record without a live engine process (or vice-versa) is the
  central failure mode; heartbeats + the placer's reconcile guarantee neither leaks.

### (4) The correctness footgun

**A zombie engine process outliving its session** (or a session marked `live` whose process died). It
burns CPU, bandwidth, and storage silently, and — for a camera with credentials — keeps an authorized
connection open with nobody watching. Every session is a lease: the engine **heartbeats**, the placer
**reconciles** (kill a process with no session, mark dead a session with no process), and the **sweeper**
is the backstop. A session's guards (TTL/idle/size) are enforced by the owner of the process, not by the
API, so they hold even if the API pod restarts.

---

## 1. Prerequisites & decisions

- **Engine = ffmpeg by default** (behind `StreamEngine`), because it opens RTSP/RTMP/HTTP, transcodes,
  segments HLS, writes MP4, and grabs frames — one dependency, no per-format code. gstreamer/null are
  alternate adapters.
- **Placement decided at the port** (`SessionPlacer`): `local` (the process runs in this pod — simple,
  ships first) or `worker-pool` (a stream-worker fleet, a registry maps session→worker, control is
  routed) — both satisfy the same interface, chosen by config. This is the answer to "stateful sessions
  break replicas": the *service* is stateless, the *placer* owns state.
- **Recordings live in file-service**, not here — we already built segmented, scanned, retained,
  content-addressed object storage; a recording segment is just a large object. stream-service holds
  *metadata* (which segments, which camera, what time range), file-service holds the bytes.
- **WebRTC needs an egress** (SFU/ICE); v1 ships an ffmpeg/`whep`-style single-origin WebRTC output
  behind the same `StreamEngine`, with `MediaEgress` config for a TURN server. LL is opt-in per source.

---

## 2. Component specs

Each subsection is a self-contained spec: **data model**, **operations**, **config**, **edge cases**.

### 2.1 Source (a camera / ingestable URL)

**What it is.** A registered, reusable definition of something to stream from. Register once, start many
sessions against it.

**Data model — `Source`:**
| field | type | notes |
|---|---|---|
| `id` | str | server-assigned |
| `tenant` | str | from token, never a param |
| `name` | str | human label |
| `url` | str (sealed) | `rtsp://…`, `rtmp://…`, `http(s)://…m3u8`; credentials sealed |
| `transport` | enum `tcp`\|`udp`\|`auto` | RTSP interleave; default `tcp` (survives NAT) |
| `kind` | enum `rtsp`\|`onvif`\|`rtmp`\|`http` | drives probing/PTZ availability |
| `onvif` | obj? | `{host, port, profile_token}` for PTZ/discovery when `kind=onvif` |
| `tags` | list[str] | grouping/filtering |
| `group_id` | str? | a "site" |
| `enabled` | bool | disabled sources refuse new sessions |
| `default_profile` | str | which transcode profile a session uses unless overridden |
| `created`, `updated` | iso | |

**Operations (tier `user`, cap `stream.manage` to mutate / `stream.view` to read):**
- `POST /api/v1/sources` — register. Validates URL (SSRF §2.13), seals credentials.
- `GET /api/v1/sources` — keyset-paged, tenant-scoped, `url` **redacted** to host only.
- `GET /api/v1/sources/{id}` · `PATCH /api/v1/sources/{id}` · `DELETE /api/v1/sources/{id}`
  (refuses delete while a session is live unless `?force=true`, which stops sessions first).
- `POST /api/v1/sources/{id}/probe` — open briefly, report codec/resolution/fps/has-audio/PTZ-capable
  **without** starting a session. Cached (`SOURCE_PROBE_TTL`).

**Config:** `SOURCE_SEED` (optional JSON list to pre-register), `RTSP_DEFAULT_TRANSPORT`,
`SOURCE_PROBE_TTL_SECONDS`.

**Edge cases:** a URL that changes credentials → re-seal, existing sessions keep the old until restart;
delete of a source with recordings keeps the recordings (they're the tenant's data) but detaches them;
a disabled source with a live session → the session runs out its guards, no new starts.

### 2.2 Session (one live stream instance) + modes

**What it is.** The Start/Stop the user clicks. A session binds a source to a set of outputs, a mode,
and a set of auto-stop guards, and is owned by exactly one engine process via the placer.

**Data model — `Session`:**
| field | type | notes |
|---|---|---|
| `id`, `tenant`, `source_id` | str | |
| `mode` | enum `on_demand`\|`viewer_driven`\|`continuous`\|`scheduled` | §2.3 |
| `status` | enum `starting`\|`live`\|`reconnecting`\|`stopping`\|`stopped`\|`error` | one-directional except reconnecting↔live |
| `outputs` | list[enum] | subset of `hls`\|`webrtc`\|`snapshot`\|`thumbnail`\|`record` |
| `profile` | str | transcode profile id |
| `guards` | obj | resolved `max_duration_s`, `max_size_bytes`, `idle_timeout_s`, `schedule` |
| `placement` | obj | `{placer, worker_id, pid/handle}` — how the placer finds the process |
| `viewers` | int | current concurrent playback tokens in use |
| `stats` | obj | `bytes_out`, `duration_s`, `fps`, `bitrate`, `reconnects`, `last_frame_at` |
| `lease_expires_at` | iso | heartbeat deadline (zombie reaping) |
| `reason` | str | why it stopped (`user`, `max_duration`, `idle`, `max_size`, `schedule`, `error`, `quota`) |
| `created`, `updated` | iso | |

**Operations:**
- `POST /api/v1/sessions` — start. Body: `source_id`, `mode?`, `outputs?`, `profile?`, guard overrides.
  Returns the session + **playback handles** (signed HLS url, WebRTC offer endpoint, snapshot url).
  Enforces quota (§2.11) and SSRF re-check.
- `GET /api/v1/sessions` / `GET /api/v1/sessions/{id}` — status + stats (cached, short TTL).
- `POST /api/v1/sessions/{id}/stop` — graceful stop (`reason=user`).
- `DELETE /api/v1/sessions/{id}` — force kill + cleanup.
- `POST /api/v1/sessions/{id}/heartbeat` (internal, engine→service) — extend the lease, push stats.

**Config:** `SESSION_DEFAULT_MODE`, `SESSION_DEFAULT_OUTPUTS`, `SESSION_LEASE_SECONDS` (heartbeat
deadline), `SESSION_START_TIMEOUT_SECONDS` (fail a session that never produces a first frame).

**Edge cases:** double-start of the same source is allowed (independent sessions) unless
`ONE_SESSION_PER_SOURCE=true`; a start that never yields a first frame within `SESSION_START_TIMEOUT` →
`error`, process killed; API pod restart → sessions keep running (owned by the placer), status re-derived
from heartbeats.

### 2.3 Session modes (the "when does it run" spec)

| mode | starts when | stops when | use |
|---|---|---|---|
| `on_demand` | user calls start | user calls stop **or any guard** | manual live view |
| `viewer_driven` | first viewer requests playback | last viewer leaves + `VIEWER_GRACE_SECONDS`, or a guard | **the "forgot to stop" fix** for live view — nobody watching ⇒ it stops itself |
| `continuous` | on create / on boot (if `enabled`) | only a guard, an explicit stop, or `enabled=false`; **auto-reconnects** | 24/7 recording |
| `scheduled` | inside `schedule` window (cron-like) | window closes, or a guard | business-hours cameras |

**Data model addition:** `schedule` = `{cron, tz, duration_s}` for `scheduled`; `viewer_driven` uses
`idle_timeout_s`/`VIEWER_GRACE_SECONDS`. **Config:** `VIEWER_GRACE_SECONDS`, `SCHEDULE_TIMEZONE_DEFAULT`.
**Edge cases:** a `viewer_driven` session with a stuck viewer token → idle is measured by *active token
heartbeats*, not by an open connection; a `continuous` session hitting `max_duration` **rolls over**
(stops, immediately restarts a new session id) so recording is unbroken but the guard still bounds any
single process.

### 2.4 Auto-stop guards (cost control — a session can never run forever)

**What it is.** The non-negotiable safety net for "he may forget to stop." Every session resolves guards
from `start override → source default → service config`, and the **process owner enforces them locally**
(so they hold without the API).

**Guards:**
| guard | stops the session when | default knob |
|---|---|---|
| `max_duration_s` | wall-clock since start exceeds it | `SESSION_MAX_DURATION_SECONDS` (e.g. 4h) |
| `idle_timeout_s` | no active viewer for this long (viewer_driven/on_demand) | `SESSION_IDLE_TIMEOUT_SECONDS` |
| `max_size_bytes` | cumulative recorded bytes exceed it | `SESSION_MAX_SIZE_BYTES` |
| `schedule` | outside the window | — |
| lease | heartbeat missed (process died) → reaped by placer/sweeper | `SESSION_LEASE_SECONDS` |

**Operations:** guards are fields on start; `PATCH /api/v1/sessions/{id}/guards` may *tighten* a running
session (never loosen past the service max). **Edge cases:** a guard of `0`/unset = "use default"; a guard
above the service hard-max is clamped (a tenant cannot ask for an unbounded stream); every guard-stop emits
an event with `reason` so the UI can explain why a stream ended.

### 2.5 Output — HLS live playback

**What it is.** Browser-compatible live view. The engine segments the stream into `.ts`/`fMP4` chunks +
a rolling `.m3u8` playlist, written to object storage; the client plays via **signed segment URLs**.

**Data model:** `HlsOutput` = `{playlist_url (signed), segment_seconds, window_segments, low_latency}`.
**Operations:** returned in the start response and at `GET /api/v1/sessions/{id}/playback` (re-issues a
fresh signed playlist token). **Config:** `HLS_SEGMENT_SECONDS` (e.g. 2), `HLS_WINDOW_SEGMENTS` (live
window length), `HLS_LOW_LATENCY` (LL-HLS on/off), `PLAYBACK_URL_TTL_SECONDS`. **Edge cases:** the app
never serves a segment (media plane); playlist token is tenant+session scoped and short-lived; a viewer
counts as active while its token is refreshed (drives `viewer_driven`).

### 2.6 Output — WebRTC low-latency

**What it is.** Sub-second live view for real-time monitoring / PTZ feel. WHEP-style: client POSTs an SDP
offer, the engine answers; media flows engine↔client directly (optionally via TURN).

**Data model:** `WebrtcOutput` = `{whep_url, ice_servers, has_audio}`. **Operations:**
`POST /api/v1/sessions/{id}/webrtc/offer` (SDP in → SDP answer out), tokened like HLS. **Config:**
`WEBRTC_ENABLED`, `TURN_URLS`, `TURN_SECRET` (sealed), `WEBRTC_MAX_VIEWERS_PER_SESSION`. **Edge cases:**
no TURN configured → host/srflx only (works on flat networks, may fail across strict NAT — reported, not
silently broken); each answered offer is a viewer for idle accounting.

### 2.7 Output — snapshot + thumbnail

**What it is.** A single JPEG *now* (snapshot) and periodic preview frames (thumbnail) — cheap, used for
grids, motion cards, and "is this camera OK" checks.

**Data model:** `Snapshot` = `{url, taken_at, width, height}`; thumbnails are periodic snapshots stored
with the session/recording. **Operations:** `GET /api/v1/sessions/{id}/snapshot` (live grab, cached
`SNAPSHOT_MIN_INTERVAL`), and `POST /api/v1/sources/{id}/snapshot` (one-off grab without a full session —
opens, grabs, closes). **Config:** `SNAPSHOT_MIN_INTERVAL_SECONDS`, `THUMBNAIL_INTERVAL_SECONDS`,
`SNAPSHOT_MAX_WIDTH`. **Edge cases:** snapshot on a source with no active session spins a throwaway
grab bounded by `SESSION_START_TIMEOUT`; snapshots are objects in file-service like everything else.

### 2.8 Output — recording (segmented → file-service, retained)

**What it is.** The surveillance core. Continuous or event-triggered recording, segmented so a long
recording is many bounded objects (and a crash loses at most one segment).

**Data model — `Recording`:** `{id, tenant, source_id, session_id, started, ended, segments:[{file_id,
start, end, bytes}], total_bytes, trigger (continuous|motion|manual), locked (legal hold)}`. Each segment
is a **file-service object** (id stored here; bytes there — reuses Phase 34 large-object path, scanning,
content-addressing, retention).
**Operations:** `GET /api/v1/recordings` (by source/time-range, keyset), `GET /api/v1/recordings/{id}`,
`GET /api/v1/recordings/{id}/segments/{n}` (signed file-service url), `POST /…/export` (§2.9),
`POST /…/lock` / `unlock` (legal hold), `DELETE /…/{id}` (respects lock). **Config:**
`RECORD_SEGMENT_SECONDS`, `RECORD_FORMAT` (fmp4|mp4|mkv), `RECORD_ON_START` (bool),
`RECORDING_FILE_SERVICE_URL`. **Edge cases:** a segment upload failure retries via the queue; a locked
recording is exempt from retention; deleting a source never orphans recordings.

### 2.9 Clip export (time-range extraction)

**What it is.** "Give me 14:03–14:07 from camera 2 as one file." Concatenates/cuts the overlapping
segments into a single downloadable object.

**Data model:** `Export` = `{id, source_id, start, end, status (pending|ready|failed), file_id}`.
**Operations:** `POST /api/v1/recordings/export {source_id,start,end,format}` → async job (queue), poll
`GET /api/v1/exports/{id}`; result is a file-service object. **Config:** `EXPORT_MAX_RANGE_SECONDS`.
**Edge cases:** range spanning a gap → export marks the gap in metadata, never fabricates frames; range
over `EXPORT_MAX_RANGE` is refused.

### 2.10 Events & triggers (motion, lifecycle) → webhook/notify/EventBus

**What it is.** The stream reacting to the world and telling other services.

**Emitted events (EventBus):** `stream.session.started/stopped/error`, `stream.source.online/offline`,
`stream.motion.detected`, `stream.recording.segment`. **On-event actions (config-declared, per source or
tenant):** start recording, take snapshot, POST to **webhook-service**, send via **notification-service**.
**Data model — `Trigger`:** `{on: event, do:[record|snapshot|webhook|notify], debounce_s, targets}`.
**Operations:** `GET/PUT /api/v1/sources/{id}/triggers`. **Config:** `MOTION_DEBOUNCE_SECONDS`,
`EVENT_WEBHOOK_ENABLED`, `EVENT_NOTIFY_ENABLED`. **Edge cases:** motion debounce prevents a storm; a
webhook/notify failure is the other service's retry problem, never blocks the stream.

### 2.11 Quotas & concurrency (per-tenant fairness)

**What it is.** One tenant (or one runaway UI) must not exhaust the engine fleet or the storage bill.

**Data model — `Quota` (per tenant, resolved from config + optional override):** `{max_concurrent_sessions,
max_stream_hours_month, max_storage_bytes}`. **Enforced at:** session start (concurrency + hours),
recording write (storage). Backed by the **entitlement** pattern (ask billing/`incr_if_below`, fail-closed)
so it composes with the platform's metering. **Config:** `STREAM_MAX_CONCURRENT_SESSIONS_GLOBAL`,
`STREAM_MAX_CONCURRENT_SESSIONS_PER_TENANT`, `STREAM_MAX_HOURS_MONTH`, `STREAM_MAX_STORAGE_BYTES`.
**Edge cases:** at the cap, start returns `429`/`quota` with the limiting dimension; hours accrue against
the subscription period (new period resets, no cron — the Phase 25 lesson).

### 2.12 PTZ control (ONVIF)

**What it is.** Move a physical camera: pan/tilt/zoom, presets. Only for `kind=onvif` sources that advertise
PTZ (from probe).

**Data model:** `PtzCapability` `{move, zoom, presets:[{token,name}]}`. **Operations (cap `stream.manage`):**
`POST /api/v1/sources/{id}/ptz/move {pan,tilt,zoom, continuous|relative|absolute}`,
`POST /…/ptz/stop`, `GET/POST /…/ptz/presets`, `POST /…/ptz/goto/{token}`. Behind a `PtzController` port
(`onvif`|`null`). **Config:** `PTZ_DRIVER`, `PTZ_MOVE_TIMEOUT_SECONDS`. **Edge cases:** a non-PTZ camera →
`409 PTZ_UNSUPPORTED` (from probe, not a failed move); continuous move auto-stops after
`PTZ_MOVE_TIMEOUT` (a dropped "stop" must not spin the motor forever — the physical forgot-to-stop).

### 2.13 Source security — SSRF guard + credential sealing

**What it is.** The source URL and ONVIF host are attacker-influenced input; this is the CWE-918 guard,
reused conceptually from webhook/connector.

**Spec:** on register **and every (re)connect**, resolve the host, require it to be in
`STREAM_ALLOWED_HOSTS`/CIDRs (if set) and NOT in private/loopback/link-local/metadata ranges (unless
`STREAM_ALLOW_PRIVATE=true` for on-prem NVRs), pin the resolved IP for the connection (DNS-rebinding safe).
Credentials are **sealed** (AES-GCM, `SEAL_KEY`) at rest and **redacted** everywhere (logs, manifest,
events, HLS playlists, error messages). **Config:** `STREAM_ALLOWED_HOSTS`, `STREAM_ALLOW_PRIVATE`,
`SEAL_KEY`. **Edge cases:** on-prem cameras are private-range by nature → `STREAM_ALLOW_PRIVATE=true` is a
supported, deliberate posture (not a bypass), still blocking metadata IPs.

### 2.14 Signed playback (a live view is not public)

**What it is.** HLS playlists, WebRTC endpoints, snapshots, and segment URLs are gated by a short-lived,
tenant+session-scoped token — the media plane is off the app but not open to the world.

**Spec:** `SignedPlayback` port (`hmac` default) mints `{session, output, exp}`; the segment/playlist
origin verifies it. A viewer "refreshes" its token to stay counted as active (drives `viewer_driven`
idle). **Config:** `PLAYBACK_URL_TTL_SECONDS`, `PLAYBACK_SIGNING_SECRET`. **Edge cases:** token TTL is
short and refreshed, so a leaked URL expires fast; revoking a session invalidates its tokens.

### 2.15 Health & reconnect

**What it is.** Cameras are unreliable; a session survives blips and reports honestly.

**Spec:** per-source health `{reachable, last_frame_at, fps, bitrate, reconnects, circuit}`; the engine
**auto-reconnects with full-jitter backoff** up to `MAX_RECONNECTS`, opening a **circuit** on a persistently
dead source (stops hammering, marks `offline`, emits `stream.source.offline`). `GET /api/v1/sources/{id}/health`.
**Config:** `RECONNECT_BACKOFF_MS`, `RECONNECT_MAX`, `SOURCE_OFFLINE_AFTER_SECONDS`. **Edge cases:** a
`continuous` session on an offline camera stays in `reconnecting` (not `error`) and resumes when the camera
returns; recording gaps are recorded as gaps.

---

## 3. Ports (plug-and-play adapters)

| port | drivers | responsibility |
|---|---|---|
| `SessionPlacer` | `local` \| `worker-pool` | start/stop/heartbeat/reconcile a session's process; owns placement state (§1) |
| `StreamEngine` | `ffmpeg` \| `gstreamer` \| `null` | open the source; produce HLS/WebRTC/snapshot/recording; report stats |
| `PtzController` | `onvif` \| `null` | move/zoom/preset |
| `MotionDetector` | `ffmpeg-scene` \| `none` | emit motion events from the stream |
| `Discovery` | `onvif-ws` \| `none` | find cameras on a network (§4) |
| `RecordingSink` | `file-service` \| `s3` \| `local` | where segments are stored (reuses ObjectStore) |
| `SignedPlayback` | `hmac` | mint/verify playback tokens |
| `MediaEgress` | `direct` \| `turn` | WebRTC ICE/TURN config |

Reused platform ports: `DataStore`, `Cache`, `EventBus`, `Queue`, `RateLimiter`, `TokenVerifier`,
`Secrets`, plus HTTP clients to file-service / webhook-service / notification-service / billing (quota).

---

## 4. Steps

| # | Step |
| --- | --- |
| 35.1 | Skeleton from `_template`; config (all §Config knobs, zero-hardcoded); `SEAL_KEY`/SSRF validators + boot guards |
| 35.2 | Ports (all of §3) + null/loopback adapters so the service boots and tests with zero infra |
| 35.3 | `StreamEngine=ffmpeg` adapter: open RTSP → HLS segments + snapshot + recording + stats; `null` engine for tests (synthetic frames) |
| 35.4 | `SessionPlacer=local` adapter: subprocess lifecycle, heartbeat lease, reconcile (kill orphan / mark-dead) |
| 35.5 | Domain: `Source`/`Session`/`Recording`/`Export`/`Trigger`/`Quota` models + stores (keyset-paged, sealed creds) |
| 35.6 | Core `StreamService`: register/probe sources; start/stop sessions with modes + **guards** + quota; heartbeat |
| 35.7 | Outputs: HLS playlist issuing + signed playback; snapshot/thumbnail; recording→file-service (segment upload via queue) |
| 35.8 | WebRTC output (WHEP offer/answer) + `MediaEgress` TURN config; viewer accounting for `viewer_driven` |
| 35.9 | Motion (`MotionDetector`) + triggers → EventBus + webhook-service + notification-service |
| 35.10 | PTZ (`PtzController=onvif`) + ONVIF `Discovery` |
| 35.11 | Retention + clip export + legal-hold; the **session sweeper** + **retention sweeper** (CronJobs, via gen_deploy_values) |
| 35.12 | Routes (all §2 endpoints) + internal (heartbeat, reconcile, sweep) + manifest (+ `admin_actions`) + wiring |
| 35.13 | Tests: unit (guards, modes, quota, SSRF, sealing, reconnect), integration (start→playlist→stop, viewer_driven idle-stop), **hot-path benchmark** (0 media bytes through the app), knob-guard, probe-contract |
| 35.14 | Docs (README + `docs/contracts/stream-service.openapi.yaml`) + live boot against a test RTSP (ffmpeg `testsrc` → local RTSP) + verify |

---

## 5. Config (every knob has a consumer)

**Drivers:** `STREAM_ENGINE_DRIVER`, `SESSION_PLACER_DRIVER`, `PTZ_DRIVER`, `MOTION_DRIVER`,
`DISCOVERY_DRIVER`, `RECORDING_SINK_DRIVER`, `MEDIA_EGRESS_DRIVER`, `FFMPEG_PATH`.
**Sessions/guards:** `SESSION_DEFAULT_MODE`, `SESSION_DEFAULT_OUTPUTS`, `SESSION_MAX_DURATION_SECONDS`,
`SESSION_IDLE_TIMEOUT_SECONDS`, `SESSION_MAX_SIZE_BYTES`, `SESSION_LEASE_SECONDS`,
`SESSION_START_TIMEOUT_SECONDS`, `VIEWER_GRACE_SECONDS`, `ONE_SESSION_PER_SOURCE`,
`SCHEDULE_TIMEZONE_DEFAULT`.
**Outputs:** `HLS_SEGMENT_SECONDS`, `HLS_WINDOW_SEGMENTS`, `HLS_LOW_LATENCY`, `WEBRTC_ENABLED`,
`WEBRTC_MAX_VIEWERS_PER_SESSION`, `TURN_URLS`, `TURN_SECRET`, `SNAPSHOT_MIN_INTERVAL_SECONDS`,
`THUMBNAIL_INTERVAL_SECONDS`, `SNAPSHOT_MAX_WIDTH`, `RECORD_SEGMENT_SECONDS`, `RECORD_FORMAT`,
`RECORD_ON_START`, `RECORDING_FILE_SERVICE_URL`, `TRANSCODE_PROFILES` (JSON).
**Retention/export:** `RETENTION_DAYS`, `RETENTION_MAX_BYTES`, `EXPORT_MAX_RANGE_SECONDS`,
`UPLOAD_SWEEP` / `SESSION_SWEEP` intervals (deploy cron).
**Quota:** `STREAM_MAX_CONCURRENT_SESSIONS_GLOBAL/_PER_TENANT`, `STREAM_MAX_HOURS_MONTH`,
`STREAM_MAX_STORAGE_BYTES`.
**Security:** `STREAM_ALLOWED_HOSTS`, `STREAM_ALLOW_PRIVATE`, `SEAL_KEY`, `PLAYBACK_URL_TTL_SECONDS`,
`PLAYBACK_SIGNING_SECRET`.
**Health:** `RECONNECT_BACKOFF_MS`, `RECONNECT_MAX`, `SOURCE_OFFLINE_AFTER_SECONDS`,
`SOURCE_PROBE_TTL_SECONDS`, `MOTION_DEBOUNCE_SECONDS`, `PTZ_MOVE_TIMEOUT_SECONDS`.

---

## 6. Threat model

- **T1 SSRF via source/ONVIF URL (CWE-918) — CRITICAL.** §2.13 allowlist + private/metadata block +
  IP-pin on every reconnect.
- **T2 Credential leakage (CWE-522/532) — HIGH.** Seal at rest; redact from logs, manifest, events,
  playlists, errors. A playlist or snapshot URL never carries `user:pass`.
- **T3 Unauthorized playback (CWE-639) — HIGH.** Signed, short-lived, tenant+session-scoped tokens; the
  media origin verifies; no public segment.
- **T4 Resource exhaustion / cost blowout (CWE-400) — HIGH.** The guards (§2.4) + quotas (§2.11) + the
  sweeper; no session is unbounded, no tenant is unbounded.
- **T5 Zombie/orphan process (correctness/cost) — HIGH.** Lease + heartbeat + placer reconcile + sweeper
  (§0.4); neither a process nor a session can outlive the other.
- **T6 PTZ runaway (physical) — MEDIUM.** Continuous move auto-stops after `PTZ_MOVE_TIMEOUT`.
- **T7 Malicious stream content (CWE-434) — MEDIUM.** Recording segments go through file-service, which
  scans; the engine is sandboxed (non-root, dropped caps) and bounded (`SESSION_START_TIMEOUT`, size caps).
- **T8 Tenant crosstalk — HIGH.** Every source/session/recording is tenant-scoped from the token; cross
  access is 404 (hidden), never 403.

---

## 7. Out of scope (deferred)

Multi-camera video walls / mosaics (a client concern) · AI object detection (a consumer of our
frames/snapshots, not this service) · cloud-provider camera SDKs (add as `StreamEngine` adapters when
needed) · a full media CDN (segments are file-service objects; front them with any CDN) · two-way audio /
intercom · RTSP *server* (re-publishing as an RTSP origin) beyond the WebRTC/HLS egress.

## 8. Why this is plug-and-play + smart

One config changes a one-camera SMB (`SESSION_PLACER=local`, HLS only, record off) into a thousand-camera
operator (`SESSION_PLACER=worker-pool`, WebRTC + continuous record + motion + retention) — same code. Every
capability is a port with a null adapter, so the service boots and tests with **zero infra** (null engine =
synthetic frames), and a customer swaps ffmpeg for their own engine, or file-service for their own sink,
without touching the core. The guards make it **safe by construction** — the thing users fear (a stream they
forgot, a bill that runs) cannot happen, because a session is a lease, not a promise.
