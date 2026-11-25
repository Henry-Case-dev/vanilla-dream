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
| `backend/app/queue_engine.py` | source resolution (override → schedule → rotation → default playlist → all library), shuffle cycles, repeat modes, manual positions, generation of the next positions, playlist rotation (sequential / by-count / random) with persisted counters, engine queue file and per-position symlinks |
| `backend/app/scheduler.py` | weekly rules in the station timezone, overnight windows, priorities, overrides with expiry |
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

Each generated position is a row in `queue_positions` with a stable UUID. Before a
position is prepared for the engine the backend creates a symlink
`/run/vanilla-dream-radio/queue/<uuid>.mp3` to the real audio file and writes that
path into `engine-queue.txt`; Liquidsoap plays the symlink and reports the exact
`filename` back through `on_track`. "Current track" is therefore an observed fact,
not an estimate, and it survives renames inside the music directory. (The
`annotate:` request protocol cannot be used: Liquidsoap 2.2.4 rejects it for a
non-root user.) Positions the engine has already received (`prepared`) cannot be
reordered in the UI; only `pending` positions can.

## Sources and the system playlist

The station always has a source decision, made in the backend:

1. active manual override (an explicit «В эфир» on a playlist or an override scope);
2. active schedule rule (highest priority, deterministic tie-break);
3. enabled rotation with at least one usable item;
4. the playlist explicitly set as the default;
5. **«Вся медиатека»** — a system playlist that always exists and always contains
   every available track of the library (virtual membership, no duplicated rows);
6. technical silence.

«Вся медиатека» is used when nothing else is configured for the current moment; it
is not a way to ignore a deliberately chosen source. A user playlist that was
chosen but is empty or has finished without repeat is reported as such
(`no_music` with a reason) instead of silently switching to the whole library.
Rotation and the schedule may move to their next eligible item by design.

The system playlist ships with shuffle and repeat-all enabled so a fresh
installation plays music as soon as files exist; the owner can change both, and
the choice persists. It cannot be deleted and its membership cannot be edited —
removing music is done in the library, which immediately updates the playlist.

## Failure behaviour

- Backend down: Liquidsoap keeps playing the prepared requests. On the next start
  the backend resets prepared positions to `pending`, re-reads the playback state
  and history, and warns when the stored track disagrees with the Icecast title.
- Engine down: the mount disappears, the API still works and reports
  `reconnecting`/`error`; the owner can restart from the panel.
- Empty library or queue: the mount stays alive with technical silence and the
  station reports `no_music`.
- Icecast statistics unavailable: the API reports `stats_available: false`
  instead of fake zeros.
