# Release evidence — Vanilla Dream Radio 1.2.1

Candidate and production commit: `6962dc99638d8cb212e8a497d796ba2b86283839`
(base `06b3c93`, VERSION 1.2.1).

## What changed (public page only)

- Removed the local-playback explanations: «Звук только в вашем браузере»,
  «Играет локально», the «локально» badge on the cover. Idle shows the round Play
  with «Слушать эфир»; playing shows the round control with «Слушаете эфир» and
  «Остановить прослушивание». Functional states (connecting, buffering,
  reconnecting, station stopped, unavailable, autoplay blocked), cancel, volume
  and reconnect are unchanged.
- One links heading «Прямые ссылки для интернет-радиоплееров и совместимых
  приложений»; subtitle and the duplicate MP3 description removed; MP3/M3U/PLS
  cards, copy and open actions kept; the public-address line and its copy button
  removed (the backend setting and links still use the public base URL).
- History keeps «Недавно в эфире», the list, order, API and live updates; only
  the subtitle «Последние треки и время выхода» was removed. History now
  refetches on `history_revision`/track start without a reload.
- The top «Вход в панель» link is gone in every page state; the footer
  «Панель управления» remains visible, keyboard reachable on desktop and mobile
  and leads to the existing `/admin` login/session. `/admin`, the login form and
  the session mechanism are untouched.
- Footer text is «Vanilla Dream Radio»; SSE, polling and all live updates stay.
- Listener count uses `pluralRu`: 1 слушатель, 2 слушателя, 5 слушателей,
  11 слушателей, 21 слушатель.
- Factual timestamps (public history, admin history, current track start, last
  service start) render in the browser's local timezone via `Intl`; the station
  timezone still defines schedule rules, weekday, next switch and schedule form
  fields. History shows `HH:mm` 24h, sorted by absolute timestamps. API
  timestamps remain ISO-8601 UTC (`…Z`); no migration.

## Verification

| Check | Result |
| --- | --- |
| `npx tsc --noEmit` | clean |
| `npx vitest run` | 53 passed |
| `npx playwright test` | 38 passed |
| `npm run build` | clean |
| Visual (Playwright + Browser Use) | 1920×1080 / 1366×768 / 390×844: no overflow, heading wraps without clipping, no empty gaps, no console errors |
| Production real browser (in-app Chrome) | header without «Вход в панель»; player «Слушать эфир»; one links heading with MP3/M3U/PLS; six real history rows with local `22:47…` for `10:47Z` (browser UTC+12); footer «Vanilla Dream Radio» + «Панель управления» → working `/admin`; account session present; `/admin/login` serves the form |
| Production admin | «Начало: 22:47» local while the schedule timezone stays `Asia/Magadan` and the schedule page shows station time |
| Served bundle | `index-5K0gm075.js` contains the new heading/labels; none of the removed texts |
| Station | API/Liquidsoap/Icecast active, `on_air` in playlist mode, history timestamps `…Z`, `history_revision`/`queue_revision` present |
| Data | tracks/playlists/history/settings unchanged; only frontend files and the version deployed |
| Adminbot | active, healthz `2.58.252` before and after the deploy |
| Backups | automatic pre-deploy SQLite backup in `/var/backups/vanilla-dream-radio/pre-deploy` |

## Backlog (non-blocking)

- Browser-level assertion for the listener plural and an explicit polling-fallback
  data test.
- Pre-existing public tooltips («Движок сообщает текущий трек», «Громкость
  локального плеера»).
- The playing glyph remains the baseline stop-square; labels/tooltip follow the
  contract.
