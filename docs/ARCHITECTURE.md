# Architecture

## Data flow

```
Browser / VLC / Minecraft World Radio
        │  HTTPS / HTTP (only entry point)
        ▼
      Caddy ──────────────► /radio.mp3 ──► Icecast 127.0.0.1:8101
        │                                   ▲  one encoded MP3 stream
        ├──────────────► /api/* ──► FastAPI 127.0.0.1:8100
        │                                   │  writes engine-queue.txt,
        │                                   │  sends `vd_sync` over a UNIX socket
        │                                   ▼
        └──────────────► static frontend   Liquidsoap (unprivileged)
                                            │  request.queue + on_track
                                            └─ POST /api/internal/engine/track-start
```

The backend never touches audio: it decides *what* should play and Liquidsoap
decides *how* it is decoded and encoded. Every listener receives the same stream.

## Components

| Component | Responsibility |
| --- | --- |
| `backend/app/queue_engine.py` | source resolution (override → schedule → rotation → default playlist → all library), shuffle cycles, repeat modes, manual positions, generation of the next positions |
| `backend/app/scheduler.py` | weekly rules in the station timezone, overnight windows, priorities, overrides with expiry |
| `backend/app/rotation.py` | sequential / by-count / random playlist rotation with persisted counters |
| `backend/app/station.py` | desired state vs unit state vs stream availability, allow-listed systemctl calls |
| `backend/app/media.py` | streaming upload, SHA-256 dedupe, Mutagen tags, cover extraction, safe delete |
| `backend/app/liquidsoap.py` | engine queue file + `vd_sync` command, skip, honest error reporting |
| `backend/app/icecast.py` | listener/bitrate statistics, `stats_unavailable` when Icecast cannot answer |
| `radio/liquidsoap.liq` | playback, MP3 encoding, track-start reporting, silence fallback |

## State model

`desired_state` (`playing|stopped`) is stored in `settings`. Actual state is
computed on every read:

| State | Condition |
| --- | --- |
| `off` | desired state is stopped |
| `starting` | unit active, mount missing for less than 30 s |
| `on_air` | mount present and a current track is known |
| `no_music` | mount present, nothing playable in the queue |
| `reconnecting` | unit active, mount missing for more than 30 s |
| `error` | unit failed or restarted too often |

A rule can never start a station the owner stopped.

## Queue positions

Each generated position is a row in `queue_positions` with a stable UUID that is
written into the Liquidsoap request as `annotate:vd_id="<uuid>"`. When the engine
starts that request it reports the UUID back, so "current track" is an observed
fact, not an estimate. Positions the engine has already received (`prepared`)
cannot be reordered in the UI; only `pending` positions can.

## Failure behaviour

- Backend down: Liquidsoap keeps playing the prepared requests; the queue is
  reconciled from the engine state on the next start.
- Engine down: the mount disappears, the API still works and reports
  `reconnecting`/`error`; the owner can restart from the panel.
- Empty library or queue: the mount stays alive with technical silence and the
  station reports `no_music`.
- Icecast statistics unavailable: the API reports `stats_available: false`
  instead of fake zeros.
