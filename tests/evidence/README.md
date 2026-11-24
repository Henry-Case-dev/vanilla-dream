# Evidence

Release evidence for this project is kept twice:

- **Private, full detail** — `plans/features/vanilla-dream-radio/evidence/` in the
  maintainer's planning checkout (not published; contains host details and
  operational notes).
- **Public, reproducible** — this file: the commands used for acceptance and the
  results observed on the production host.

## Reproduce the checks

```bash
# backend behaviour (no external services)
cd backend && .venv/Scripts/python -m pytest -q

# frontend build and type check
cd frontend && npx tsc --noEmit && npm run build

# on the production host: real audio, metadata, listeners, Adminbot health
python3 tests/e2e/server_check.py

# on the production host: full product flow (login, upload, playlist,
# schedule, queue, station start, skip, history, services)
python3 tests/e2e/api_flow.py --password-file /root/vdr-password \
  --fixtures /tmp/vdr-fixtures
```

`make_fixtures.py` generates three short technical MP3s (Cyrillic tags, one with
an embedded cover) used by the flow above; they must be deleted from the library
after the checks.

## Results recorded for the release

| Check | Result |
| --- | --- |
| `pytest -q` | 140 passed |
| `tsc --noEmit` / `npm run build` | clean, bundle built |
| `server_check.py` | stream decodes to >1 s of PCM, ICY `br=128`, two concurrent listeners, Adminbot healthy |
| `api_flow.py` | 26/26 checks passed against production |
| Backup | daily timer, restorable SQLite snapshot, retention 7 |
| Adminbot | `admin_bot.service` active, `healthz` ok, before and after every deploy |

Screenshots of the real product are in `docs/screenshots/`.
