# AGENTS.md — Vanilla Dream Radio

Self-hosted internet radio station: Liquidsoap encodes, Icecast distributes, a
FastAPI backend owns the queue, rotation and schedule, a React panel and a public
player are the only user interfaces.

## Rules

- **Never touch Adminbot** (`C:\Code\Python\adminbot`, `admin_bot.service`, its
  Caddy site, database, ports or secrets). It shares the VPS and must keep
  working. Radio has its own user `vdradio`, units `vanilla-dream-radio-*`,
  database, venv, directories and Caddy site file.
- **One shared stream.** MP3 CBR (default 128 kbps) stereo 44.1 kHz on
  `/radio.mp3`. Icecast and Liquidsoap listen on localhost only; the backend
  listens on 127.0.0.1:8100. Never publish the Icecast admin, the Liquidsoap
  command socket or the backend port directly.
- **The backend owns programming.** Queue, shuffle, repeat, rotation, schedule
  and manual overrides are decided in Python. Liquidsoap only plays the prepared
  requests and reports real track starts through `on_track`. Do not add a second
  shuffle/repeat inside Liquidsoap.
- **One encoder.** No per-listener transcoding, no extra bitrates, no HLS.

## Secrets

Never commit `.env`, passwords, tokens, keys, the DuckDNS token, the database or
music. Production secrets live only in `/etc/vanilla-dream-radio/env` (0640
root:vdradio) and `/etc/vanilla-dream-radio/duckdns.env` (0600 root). The
repository is public: check `git diff --cached` before every commit.

## Commands

```bash
# backend tests (local)
cd backend && python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m pytest -q

# frontend build
cd frontend && npm ci && npm run build

# first installation on the VPS (idempotent)
ssh root@192.255.209.205 "bash /opt/vanilla-dream-radio/current/deploy/install.sh --sha <sha>"

# deploy a new commit (builds frontend locally, pushes, updates the server)
pwsh deploy/deploy.ps1

# runtime checks
curl -s http://127.0.0.1:8101/status-json.xsl | head -c 400
curl -s http://127.0.0.1:8100/api/public/now-playing
systemctl status vanilla-dream-radio-api vanilla-dream-radio-icecast
```

## Deployment rules

- Production must correspond to a specific Git commit (`/opt/vanilla-dream-radio/current`).
- Re-running `deploy/install.sh` or `deploy/update.sh` must never recreate the
  database, delete music or overwrite real settings with templates.
- Any Caddy change: validate, reload, then verify Adminbot health **and** radio
  endpoints; roll back only our own site file on failure.
- The engine is started by the backend only when `desired_state=playing`.

## Visual verification is mandatory

Any frontend change is checked in a real browser (desktop 1920×1080, 1366×768,
mobile 390 px) with screenshots, no console errors, and the states
loading/empty/error. Do not claim a UI change works from source alone.
