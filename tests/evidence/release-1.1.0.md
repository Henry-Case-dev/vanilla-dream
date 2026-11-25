# Release evidence — Vanilla Dream Radio 1.1.0

Committed with the release it describes; requirement maps and the release gate
bind to this commit.

## What this release adds

- **Duplicate-aware import.** A received file is streamed to a pending area,
  probed and hashed. If it matches an existing record by content (same SHA-256)
  or by normalised tags (title+artist, album equal or differing) it waits for a
  decision instead of being rejected or silently overwriting anything. Pending
  files are invisible to the library, the system playlist, the queue and the
  public API; confirmation creates an independent record.
- **Independent storage.** New files live at `music/<track_id><ext>`; `sha256` is
  an indexed detection tool, not a uniqueness constraint (non-destructive
  migration, `user_version` 3). Deleting or editing one record never affects
  another with the same hash.
- **Batch decisions.** One dialog compares the existing and the new file, per-row
  «Пропустить»/«Сохранить копию», bulk actions that apply only to undecided items,
  honest counters and idempotent confirmation.
- **Track editor.** Title, artist, album and a custom cover with «Вернуть
  исходную», applied without rewriting the source audio file.
- **Downloads.** A single track with current tags (a temporary copy with rewritten
  ID3/APIC, no re-encoding) or as the original file; a selection, a playlist or the
  whole library as a streamed `ZIP_STORED` archive with `playlist.m3u8`, unique
  safe names and duplicates kept as separate entries; or as separate files.
  One archive at a time, bounded memory, disk guard, automatic expiry.
- **Duplicate badge** «Дубль» beside the title, persisted independently of later
  renames, never added to public/ICY metadata.

## Verification

| Check | Result |
| --- | --- |
| backend `pytest -q` | 213 passed |
| frontend `npm test` | 28 unit + 11 Playwright |
| `tsc --noEmit`, `npm run build` | clean |
| `tests/e2e/media_flow.py` (isolated, real backend/engine) | all checks pass; 32/32 on a clean isolated station (the count depends on how many matches the batch produces) — duplicates, decisions, editor, single download with ID3 read-back, playlist ZIP with duplicates and m3u8, selection export, station stays on air |
| `tests/e2e/playwright_flow.py` | 51/51 on a fresh station; on a station whose short test playlist finishes before the override banner is read, the banner checks are timing-dependent (the banner itself is confirmed in the UI) |
| `tests/e2e/stress_checks.py` | 12/12 when the station is already playing (the flow expects live audio) |
| Production migration | v2 → v3 with a verified backup: 29 tracks / 237 906 336 B, «Приснись нам, Земля» with 29 relations, history and settings preserved, station back on air |
| Production download | `GET /tracks/{id}/download?mode=tags` returns the real MP3 with `Content-Disposition: attachment`, ID3 title/artist and an embedded APIC; `mode=original` works |
| Adminbot | active, healthz ok before and after the deploy |

## Known limitations

- Automated export-tag assertions use MP3; other formats are handled by the same
  code path but covered by unit tests only.
- Pending uploads expire after 24 h and archives after 6 h of inactivity.
- One archive is prepared at a time; further exports queue.
