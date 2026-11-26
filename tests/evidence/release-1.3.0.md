# Release evidence — Vanilla Dream Radio 1.3.0

Candidate and production commit: `6a4c865142fcb25e7515c84fc296ad2fbbda922e`
(base `a898d04`, VERSION 1.3.0). Deployed with `deploy/update.sh`; automatic
pre-deploy SQLite backup `app-20261126T202933Z.db`.

## What this release adds

- **Third duplicate decision — replace.** The new file becomes the audio file of
  one explicitly selected existing track. The track id, playlist membership and
  order, system playlist, history, creation date, user-edited metadata, custom
  cover and historical duplicate marker are preserved; the library count does not
  grow. Technical fields (`sha256`, size, duration, bitrate, ext, path) and
  `source_*` tags come from the new file; a missing new source tag does not erase
  a useful display value.
- **Safe file switch.** The upload is staged and copied to a new independent
  `music/<track_id>.<random><ext>` path outside SQLite transactions; a short
  `BEGIN IMMEDIATE` re-validates the target (exists, not deleted, same
  `sha`/`path` as at detection) and switches the row atomically. The old path goes
  to `retained_files` and is deleted only after the engine, an active export
  snapshot and open downloads release it; failures keep the old track usable and
  retry cleanup.
- **Playback semantics.** The current instance finishes the old file with its
  original duration/progress (`playback_state.duration` snapshot); future and
  prepared positions use the new file after a confirmed forced sync. Repeat One,
  cycles and shuffle progress are unchanged. No service restart, skip or browser
  reconnect.
- **Metadata and cover.** Display fields the user edited stay; auto-extracted
  values and the embedded cover update. «Вернуть исходную» and original download
  use the new file. Cover URLs are versioned by `updated_at`/`replaced_at` plus a
  short content token, so the row and player refresh without a reload.
- **Decisions UI.** Three compact actions (skip / replace with RefreshCw / save
  copy), lilac selection, short replace explanation (plus «Текущий трек доиграет
  до конца» for the playing track), target picker with candidate summaries when
  several tracks match, identical file disables replace with «Файлы идентичны»,
  batch skip/replace/save remaining only for undecided unambiguous targets, target
  and conflict reporting, counters and result («Добавлено · Заменено ·
  Пропущено»), stale/failed/conflict inline with retry, repeated apply idempotent,
  modal stays open and live updates keep focus/scroll.
- **Schema v5 (additive).** `tracks.replaced_at`,
  `retained_files(id, track_id, path, reason, created_at)`,
  `playback_state.duration`, `pending_uploads.match_sha/decision_target/
  decision_result`; no data change, `match_sha` backfilled for legacy pending.

## Verification

| Check | Result |
| --- | --- |
| backend `pytest -q` | **304 passed** |
| frontend `tsc --noEmit`, `npm run build` | clean |
| frontend `vitest` / `playwright` | 54 passed / 50 passed |
| independent backend review | APPROVED (300 tests) + F1 cover delta APPROVED (304 tests) |
| independent frontend review | NEEDS_FIXES → fixes → **APPROVED** (54/50, cover refresh, target selection, conflicts) |
| isolated stand (`tests/e2e/media03_flow.py`, real Liquidsoap + Icecast) | **20/20**: id/count/created/playlist order preserved, duration 18.05 s and bitrate 96 after replace, original download new bytes, engine instance unchanged, playing instance keeps 12.04 s and is not interrupted, next play 18.05 s, export created before the replace still produces its archive, identical no-op |
| isolated stand real UI (Chromium, `media03_ui.py`) | **8/8**: login, upload a new version, decisions dialog, «Заменить трек», explanation, «Применить решения», result «Заменено: 1», one library row, duration from the new file, no console errors |
| migration on a copy of the real production v4 DB | schema 5; 100 tracks, 483 history rows, 100 playlist relations preserved; new columns/table; idempotent |
| production schema/data | `user_version` 5; 100 tracks, 647 history rows, 100 playlist relations, playlists «Приснись нам, Земля» + «Вся медиатека», `playback_mode=playlist`, selected source preserved, no retained leftovers |
| production stream | `on_air`, playlist mode, real track, cover URLs include `?v=<timestamp>-<token>` in now-playing and history |
| production ICY | `Icy-Name: Vanilla Dream Radio`, 128 kbps, `audio/mpeg` |
| production pages | public 200, `/admin` 200 |
| Adminbot | active, healthz `2.58.253` before and after |
| backups | `pre-deploy/app-20261126T202933Z.db` (automatic) |

## Known limitations (backlog, non-blocking)

- After a 409 stale match the old pending cannot be re-confirmed for replace
  without a fresh upload (save/skip stay available).
- Retained-path protection against the engine relies on `os.readlink`+`resolve()`
  and is effective on Linux (production), not on a Windows dev host.
- Legacy pending with several candidates can replace only the best match.
- The admin queue snapshot shows the new duration for a replaced playing track
  while the public now-playing keeps the frozen one (contract requires the
  public/player snapshot).
- `original_cover_url` is not versioned; the editor's revert preview can show a
  stale extracted cover for up to the 5-minute cache.
- A pre-existing scroll jump to the initially opened decision row on expanding
  another row; a batch client helper exists but the dialog applies per item.
