# Release evidence — Vanilla Dream Radio 1.0.1

Committed with the release it describes; the requirement maps and the release
gate bind to this commit.

## What changed

Corrective release after real use of 1.0.0: the playlist track list was read
from the wrong place in the API payload, services and storage ignored the real
fields, the stream link block duplicated a Minecraft-specific entry, statuses
reflected the desired state instead of playback, the modal dialog lost input
focus on every keystroke, and — most importantly — the "whole library" source
used a hardcoded `repeat=none`, so after one pass the station stayed silent even
with a full library.

## Delivered

- **System playlist «Вся медиатека»** — always present, virtual membership from
  the library, shuffle + repeat-all by default, cannot be deleted or edited in
  membership, usable as the default source, in the schedule and in rotation.
- Playlist detail with tracks and counters, mass selection without a 50-track
  limit, real add counters, custom playlist covers with automatic fallback.
- Source priority: override → schedule → rotation → default playlist → system
  playlist → technical silence; a configured but empty/exhausted source is
  reported, not silently replaced.
- Cycles: repeat-all restarts, shuffle cycles without immediate repeats,
  single-track and short playlists, new tracks join the current cycle.
- Queue/engine sync: honest `OK <resolved> <failed>`, retry after failure,
  recovery after engine restart, `current.id`.
- Statuses with reasons (`library_empty`, `playlist_empty`, `playlist_exhausted`,
  `no_source`, `engine_unavailable`, `engine_sync_failed`, `playback_stalled`)
  and a start grace window instead of a transient error flash.
- Services with units, numeric storage, universal links without the duplicated
  Minecraft entry, playlist «В эфир» from a stopped station with
  «Вернуться к автоматическому».
- Modal focus: initialised once per opening, first field preferred, typing and
  cursor preserved, Escape returns focus to the real opener.

## Verification (same session, real stack)

| Check | Result |
| --- | --- |
| backend `pytest -q` | 183 passed |
| frontend `npm test` | 18 unit + 8 Playwright (focus, Escape restore, auth redirect, upload accessibility) |
| `tsc --noEmit`, `npm run build` | clean |
| `tests/e2e/playwright_flow.py` (isolated station, real Liquidsoap + Icecast) | 55/55 |
| `tests/e2e/stress_checks.py` | 12/12 (62-track select-all, real audio mean −25 dB, skip, ICY, history) |
| Production migration | 29 tracks / 237 906 336 B and the user playlist with 29 relations preserved, `user_version` 2, stale library cycle cleared |
| Production station | on air from «Вся медиатека», real track, queue, ICY title and bitrate match |
| Adminbot | `admin_bot.service` active, healthz ok before and after every deploy |

## Known limitations

- A brief `starting` state on a cold engine start is intentional; a permanent
  engine failure still surfaces as `error` after the grace window.
- Public history uses the browser timezone; the panel uses the station timezone.
- Minecraft World Radio is verified by the direct MP3 route and ICY headers, not
  inside a game client.
