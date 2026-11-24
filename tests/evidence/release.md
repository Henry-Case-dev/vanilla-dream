# Release evidence — Vanilla Dream Radio

This file is committed together with the release it describes: the evidence
records in the requirement maps and the release gate bind to that commit.

## Deployment

- Host: Ubuntu 24.04, 2 vCPU, 4 GB RAM, 50 GB free at delivery.
- Layout: `/opt/vanilla-dream-radio/current` (symlink to a release directory),
  `venv`, `frontend-dist`; music `/srv/vanilla-dream-radio/music`, covers
  `/srv/vanilla-dream-radio/covers`; database `/var/lib/vanilla-dream-radio`;
  configuration `/etc/vanilla-dream-radio`; backups
  `/var/backups/vanilla-dream-radio`.
- Units: `vanilla-dream-radio-api`, `-icecast`, `-liquidsoap` (started by the
  backend only), `-backup.timer`, all inside `vanilla-dream-radio.slice`.
- Reverse proxy: own Caddy site, `caddy validate` before reload, Adminbot health
  checked after every deploy.
- Install and update are idempotent (`deploy/install.sh`, `deploy/update.sh`);
  an update installs the units/sudoers/logrotate/tmpfiles from the release, so the
  installed configuration cannot drift from the deployed commit.

## Verification performed

| Check | Result |
| --- | --- |
| `pytest -q` (backend) | 140 passed |
| `tsc --noEmit`, `npm run build` (frontend) | clean, bundle built |
| `tests/e2e/server_check.py` on production | 15/15 — real MP3 decodes to PCM, ICY `br=128`, two concurrent listeners, Adminbot healthy |
| `tests/e2e/api_flow.py` on production | 26/26 — login, CSRF, upload, duplicate, playlist, schedule, queue, start, skip, history, links, storage, services |
| Station control | start → on air with a real track, skip advances within seconds, stop persists after an API restart |
| Engine settings | bitrate 128↔160 applied through the panel; engine restarts, ICY bitrate follows, config check passes |
| Restart with an open player stream | ~1.2 s, no SIGKILL, panel answers immediately after |
| Backup | systemd timer + restorable SQLite snapshot + retention |
| Adminbot | `admin_bot.service` active, `healthz` ok, before and after every deploy |
| Independent review | NEEDS_FIXES → fixes → reopened findings → APPROVED on the final candidate |

## Known limitations

- Minecraft World Radio was not executed in a game client; the direct MP3 URL
  (HTTP and HTTPS, no HTML, no auth) and its ICY headers were verified instead.
- The public history is rendered in the browser timezone; the panel uses the
  station timezone.
- Manual metadata edits live in the database and are used by the UI, now-playing
  and history; embedded file tags are not rewritten (out of scope).
- The production host has an empty library by design: the owner uploads music
  after delivery. With an empty library the station reports «Нет музыки» and the
  engine may be stopped.

## Reproduce

See `tests/evidence/README.md` for the exact commands and fixture generator.
