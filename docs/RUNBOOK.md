# Runbook

## Units

| Unit | Role | Enabled |
| --- | --- | --- |
| `vanilla-dream-radio-api.service` | FastAPI backend on 127.0.0.1:8100 | yes |
| `vanilla-dream-radio-icecast.service` | Icecast on 127.0.0.1:8101 | yes |
| `vanilla-dream-radio-liquidsoap.service` | encoder/engine, started by the backend when the station is on air | no |
| `vanilla-dream-radio.slice` | memory/CPU/IO limits for all radio units | — |
| `vanilla-dream-radio-backup.timer` | daily database + configuration backup | yes |

## Everyday operations

Everything below is available in the panel; the commands are for recovery.

```bash
systemctl status vanilla-dream-radio-api vanilla-dream-radio-icecast vanilla-dream-radio-liquidsoap
journalctl -u vanilla-dream-radio-api -n 100 --no-pager
journalctl -u vanilla-dream-radio-liquidsoap -n 100 --no-pager
```

### The panel says "ошибка" or "восстановление соединения"

```bash
curl -s http://127.0.0.1:8101/status-json.xsl | head -c 400   # is a mount present?
sudo -u vdradio liquidsoap --check /etc/vanilla-dream-radio/liquidsoap.liq
systemctl restart vanilla-dream-radio-liquidsoap.service
```

### Change the bitrate

Panel → Настройки → Вещание. The backend rewrites
`/etc/vanilla-dream-radio/encoder.liq` and restarts the engine; the stream
reconnects for a few seconds.

### Database backup

```bash
sudo -u vdradio env $(cat /etc/vanilla-dream-radio/env | xargs) \
  /opt/vanilla-dream-radio/venv/bin/python -m app.cli backup --dest /var/backups/vanilla-dream-radio
ls -la /var/backups/vanilla-dream-radio
```

Restore: stop the API, copy the chosen `app-*.db` over
`/var/lib/vanilla-dream-radio/app.db`, start the API.

### Deploy / rollback

```bash
# from the developer machine
pwsh deploy/deploy.ps1

# rollback to a previous release
sudo ln -sfn /opt/vanilla-dream-radio/releases/<old-sha> /opt/vanilla-dream-radio/current.tmp
sudo mv -Tf /opt/vanilla-dream-radio/current.tmp /opt/vanilla-dream-radio/current
sudo systemctl restart vanilla-dream-radio-api.service
```

### Disk space

Music lives in `/srv/vanilla-dream-radio/music`. The backend refuses uploads
when free space is below 3 GiB or above `max_library_gb`. Check with
`df -h /srv`.

## Safety notes

- Never disable `caddy`, `admin_bot.service`, fail2ban or the firewall.
- Never publish ports 8100/8101 or the UNIX socket.
- The only sudo rights of `vdradio` are start/stop/restart/is-active of
  `vanilla-dream-radio-liquidsoap.service`.
