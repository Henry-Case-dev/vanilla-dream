# Release evidence — Vanilla Dream Radio 1.2.0

Candidate and production commit: `e10c2f667fb4b96cae51582a6e3ec34d1e24bbc5`
(base `543f3e5`, VERSION 1.2.0). Deployed with `deploy/update.sh` twice: the
first run used the previous release's copy of the script, the second applied the
new script logic (new environment keys and the Icecast metadata render).

## What this release adds

- **One station mode.** `playlist` (permanent selected playlist) or `auto`
  (schedule → rotation → default playlist → «Вся медиатека»). A normal playlist
  choice no longer creates an indefinite override; overrides exist only for an
  explicit temporary action. The selected source survives stop, backend restart,
  engine restart and VPS reboot.
- **Real switching.** «В эфир» prepares and confirms the new engine queue before
  moving the playback; «После текущего трека» prepares the real head of the new
  source so the very next track belongs to it. Rapid switches end on the last
  accepted request (`state_revision`).
- **Verified sync.** `engine-queue.txt` carries `<position_id>\t<path>`; `vd_sync`
  names every accepted and rejected position, so a failed request never becomes
  `prepared`. A stale confirmation does not overwrite a newer queue. A backend
  restart keeps prepared positions; an engine restart (even while the API runs)
  is detected and resynced once.
- **Editable queue.** Every future position, prepared included, can be moved,
  moved next, removed, shuffled or cleared. Manual insertions keep their order.
- **Shuffle and repeat as player controls.** The bottom Shuffle toggles the
  permanent playlist shuffle of the effective program playlist (not
  `now.source`), Repeat cycles `none → all → one` from the same stored state,
  rotation governs repetition when it owns the source, and changing the settings
  keeps the passed part of the cycle.
- **Dynamic replenishment.** New tracks join the remaining part of the active
  pass without restarting it; an empty or exhausted active source resumes with
  the new tracks only; an inactive playlist never touches the air.
- **Web player recovery.** The provider keeps the listening intent, reconnects
  with bounded backoff after an Icecast/Liquidsoap restart without a page reload,
  never re-creates a healthy connection on a track change and shows honest
  local states (connecting, reconnecting, station off, blocked, unavailable).
- **Media safety.** Deletion removes a track from the lineup immediately, plays
  it out by default or skips on request, purges only after the engine released
  the file and keeps the row on a failed unlink. Import copies files outside the
  write transaction; duplicate-aware import stays. Exports are snapshot-protected
  and never deleted while downloading. The editor saves tags and the cover in one
  atomic operation.
- **ICY metadata from settings.** Mount defaults are rendered from the station
  settings; the brand is no longer hardcoded. The live `icy-name`,
  `icy-description`, `icy-genre`, `icy-url` and `icy-br` come from the Liquidsoap
  source built from `station.liq`.

## Verification

| Check | Result |
| --- | --- |
| backend `pytest -q` | **278 passed** |
| frontend `tsc --noEmit`, `npm run build` | clean (84.28 kB CSS / 443.72 kB JS) |
| frontend `vitest` / `playwright` | 48 passed / 27 passed |
| isolated stand (`tests/e2e/core03_flow.py`, real Liquidsoap + Icecast + Chromium) | **65/65** checks, including real playback order, no engine restart on a switch, queue editing, shuffle/repeat, replenishment, delete and player recovery after restarting both services |
| migration on a copy of the real v3 database | schema 4; 81 tracks, 181 history rows, playlists preserved; old indefinite launch override converted to playlist mode and deactivated; cycle/exclusions and queue refs moved; idempotent |
| production schema/data | `user_version` 4, 81 tracks (0 pending delete), history 219, «Приснись нам, Земля» + «Вся медиатека» intact, `playback_mode=playlist`, selected source preserved, no active override, queue current+prepared+pending |
| production stream | `on_air` from «Приснись нам, Земля», real track playing |
| production ICY (public HTTPS) | `Icy-Name: Vanilla Dream Radio` (equals the settings), description/genre/url/bitrate present, not the domain |
| production `server_check.py` (public base/stream) | **21/21** checks: API, m3u/pls, history, mount, 128 kbps, capture, decode, ICY identity, listeners, Adminbot |
| Adminbot | active, healthz ok before and after both deploy runs (`2.58.248`) |
| Caddy | reloaded with the frontend build; public admin page returns 200 |

## Backups

Automatic pre-deploy backups: `app-20261125T184904Z.db` and
`app-20261125T184924Z.db` in `/var/backups/vanilla-dream-radio/pre-deploy/`
(the directory keeps the 7 newest).

## Known limitations (backlog, non-blocking)

- A deleted pending head can return in the target playlist's next pass.
- Bulk delete of a pending head has no dedicated regression test (the shared
  path is covered by the single-delete probe).
- Public «Недавно в эфире» does not consume `history_revision` (the admin
  overview does).
- Permanent 4xx engine callbacks are logged and dropped instead of retried; 5xx
  and timeouts retry with the original timestamp.
- The scheduler refreshes the engine queue at most every 5 s even when nothing
  changed (rate limit, not a change detector).
