# stream-service

> Live camera / RTSP ingest, control, record, and view. Registers a source (RTSP/ONVIF/RTMP/HTTP)
> and turns it into a **live view** (HLS + WebRTC), **snapshots**, **recordings** (stored in
> file-service), motion **events**, and **PTZ** — tenant-scoped, capability-gated, config-driven.
> The contract is [`docs/contracts/stream-service.openapi.yaml`](../../docs/contracts/stream-service.openapi.yaml);
> the design is [`docs/phases/phase-35-stream-service.md`](../../docs/phases/phase-35-stream-service.md).

## The two guarantees

**A session can never run forever.** Every session resolves auto-stop **guards** — `max_duration`,
`idle_timeout` (no viewers), `max_size`, `schedule`, and a heartbeat `lease` — clamped to the service
maxima, plus per-tenant **quota**. A CronJob-driven sweep enforces them and reaps zombie processes
(an engine with no session is killed; a session with no engine is marked dead). This is the answer to
*"the user forgot to stop it"*.

**The bytes never touch this service.** ffmpeg writes HLS segments and recordings straight to
disk/bucket; live playback is a **signed, short-lived URL** (HLS playlist / WebRTC WHEP). The API only
ever returns a *handle*. A benchmark asserts zero media bytes flow through the app.

## API (tier `user`; caps `stream.view` read / `stream.manage` mutate / `platform.stream` operator)

| Method · Path | Purpose |
|---|---|
| `POST/GET /api/v1/sources` · `GET/DELETE /sources/{id}` | register / list / get / delete a camera |
| `POST /sources/{id}/probe` | inspect codec/resolution/fps/audio/PTZ without a session |
| `GET/PUT /sources/{id}/triggers` | motion/online/offline → record/snapshot/webhook/notify |
| `POST/GET /api/v1/sessions` · `GET/DELETE /sessions/{id}` · `POST /sessions/{id}/stop` | start / list / get / stop a live session |
| `GET /sessions/{id}/playback` | (re)issue signed HLS/WebRTC/snapshot handles; counts a viewer |
| `GET/DELETE /api/v1/recordings` · `POST /recordings/{id}/lock|unlock` | recordings + legal hold |
| `POST /sources/{id}/ptz/move|stop`, `GET /ptz/presets`, `POST /ptz/goto/{token}` | PTZ (ONVIF) |
| `POST /api/v1/discovery` | find ONVIF cameras on the network |

Internal (CronJob-driven): `POST /internal/stream/sweep` (guards + reconcile + ship + motion),
`POST /internal/stream/retention`, `POST /internal/stream/sessions/{id}/heartbeat`.

## Session modes

- `on_demand` — start/stop on click.
- `viewer_driven` — starts on first viewer, **stops when the last viewer leaves** + grace (the
  forgot-to-stop fix for live view).
- `continuous` — 24/7 with auto-reconnect (for recording); rolls over on `max_duration`.
- `scheduled` — only inside a time window.

## Plug-and-play (every capability is a port with a null adapter)

| port | drivers |
|---|---|
| `StreamEngine` | **ffmpeg** \| null (synthetic, zero-infra) |
| `SessionPlacer` | local (this pod) \| worker-pool *(same interface)* |
| `PtzController` | onvif \| null · `MotionDetector` ffmpeg-scene \| none · `Discovery` onvif-ws \| none |
| `RecordingSink` | file-service \| s3 \| local \| memory · `SignedPlayback` hmac · `MediaEgress` direct \| turn |

Set `STREAM_ENGINE_DRIVER=null` and it boots and tests with **no ffmpeg, camera, or TURN**. One
config turns a one-camera SMB into a thousand-camera operator — same code.

## Security

- **SSRF guard** on every source URL (CWE-918): scheme allow-list, metadata/loopback/link-local
  always blocked, private blocked unless `STREAM_ALLOW_PRIVATE` (on-prem NVRs), optional host/CIDR
  allowlist, IP-pinned per (re)connect (DNS-rebinding safe), re-validated on start.
- **Credentials sealed** (AES-256-GCM, `SEAL_KEY`) at rest and **redacted** from every response, log,
  event, and playlist. **Signed, short-lived playback** — a live view is not public. Tenant-isolated
  (cross access is 404, not 403). PTZ continuous move carries a **timeout** so a lost stop can't spin
  the motor forever.

## ffmpeg is an external tool

Like clamd/postgres, ffmpeg is invoked as an external binary (`FFMPEG_PATH`) — never shipped or
linked, so the permissive-license posture is unchanged. The engine/motion adapters are verified
against a real ffmpeg (infra-gated tests, `STREAM_FFMPEG_PATH`); the null path runs everywhere.

## Run

```bash
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -e ".[dev]"
uvicorn app.main:app                     # STREAM_ENGINE_DRIVER=null by default
pytest -q                                # zero-infra
STREAM_FFMPEG_PATH=/path/to/ffmpeg pytest -q   # also runs the real-ffmpeg engine/motion tests
```
