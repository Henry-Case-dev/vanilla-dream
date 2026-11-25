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

## Station modes (1.2.0)

The station has one explicit mode. `playlist` mode plays the permanently selected
playlist (`selected_playlist_id`, «Вся медиатека» on a fresh install); `auto` mode
resolves the source from the schedule, then rotation, then the default playlist,
then the system playlist. A normal playlist choice changes the mode and the
selected playlist — it does not create a manual override. Overrides exist only for
an explicit temporary action (one track, until a time, until the next schedule
change, until cancelled) and never replace the mode.

A switch writes the new state, rebuilds and confirms the engine queue, and only
then moves the playback (`now`) or schedules it for the end of the current track
(`next`, stored as `pending_source`). Every state change bumps `state_revision`, so
a late result cannot roll the station back to an older selection.

The engine instance is identified by the liquidsoap unit's start timestamp: a
backend-only restart keeps `prepared` positions, an engine restart resets them and
resyncs. `vd_sync` names every accepted and rejected position, so the backend never
reports an unplayable request as prepared.

## Cycles, shuffle and repeat

Each source keeps its cycle in `settings` (`_cycle:<source key>`): the ordered
track ids of the pass, the not-yet-played remainder, the cycle number, the last
track of the previous pass and the Repeat One target. A pass is never restarted
by a settings change: enabling shuffle reorders the remainder, disabling restores
the saved order of the remainder, `all → none` only drops positions of a future
pass, and Repeat One keeps the ordinary progress. `shuffle` and `repeat_mode` are
plain playlist columns and the only source of truth for the playlist page, the
player bar and the playlist cards.

Membership changes use the same cycle: new tracks join the remaining part of the
current pass (appended in saved order, or mixed into the unplanned remainder when
shuffle is on); already played tracks never return, and a request that adds no new
track does not rebuild or sync anything. An empty active source resumes with the
first available track; an exhausted `repeat=none` source plays only newly added
tracks and then goes silent again.

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

## Media pipeline (1.1.0)

Upload is two-phase. A received file is streamed to `music/.pending/`, probed with
Mutagen and hashed; if it matches nothing it is stored as `music/<track_id><ext>`
and inserted. If it matches an existing record by content (same SHA-256) or by
normalised tags (title+artist, album equal or differing), the file stays pending
with its own id until the owner decides «Сохранить копию» or «Пропустить». A
pending file is invisible to the library, the system playlist, the queue and the
public API. Confirmation is idempotent and creates an independent record
(`duplicate_of`, `duplicate_kind`), never overwriting the original file.

`sha256` is an indexed detection tool, not a uniqueness constraint; ids and
physical paths stay unique. Metadata edits (title, artist, album) and custom
covers live in SQLite and `covers/custom/`, never in the source audio file, and
never restart the engine.

Export is a background job: a single track is streamed (optionally from a
temporary copy with rewritten ID3/APIC, no re-encoding), while a playlist, a
selection or the whole library becomes a `ZIP_STORED` archive under
`state/exports/<job>/` with `<NNN> - Artist - Title.ext` entries and
`playlist.m3u8`. One archive runs at a time, memory stays bounded, free space is
checked before starting, and archives expire automatically.

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
