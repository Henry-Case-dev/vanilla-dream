#!/usr/bin/env bash
# Vanilla Dream Radio — idempotent installation / re-installation on the VPS.
#
# Usage:
#   sudo bash deploy/install.sh [--sha <git-sha>] [--admin-password-file <path>]
#
# Safe to run twice: secrets, database, music and settings are never recreated.
# Does not touch Adminbot, its units, configuration, ports or files.
set -Eeuo pipefail

REPO_URL="${VDR_REPO_URL:-https://github.com/Henry-Case-dev/vanilla-dream.git}"
APP_DIR=/opt/vanilla-dream-radio
ETC_DIR=/etc/vanilla-dream-radio
MUSIC_DIR=/srv/vanilla-dream-radio/music
COVER_DIR=/srv/vanilla-dream-radio/covers
STATE_DIR=/var/lib/vanilla-dream-radio
LOG_DIR=/var/log/vanilla-dream-radio
BACKUP_DIR=/var/backups/vanilla-dream-radio
RUN_DIR=/run/vanilla-dream-radio
RADIO_USER=vdradio
RADIO_GROUP=vdradio

SHA=""
ADMIN_PASSWORD_FILE=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --admin-password-file) ADMIN_PASSWORD_FILE="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"

if [[ -z "$SHA" ]]; then
  SHA="$(git -C "$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)" rev-parse HEAD 2>/dev/null || true)"
fi
[[ -n "$SHA" ]] || die "cannot determine git sha; pass --sha"

log "Installing Vanilla Dream Radio at commit $SHA"

# ---------------------------------------------------------------- packages
log "Checking system packages"
missing=()
command -v icecast2 >/dev/null || missing+=(icecast2)
command -v liquidsoap >/dev/null || missing+=(liquidsoap)
command -v ffmpeg >/dev/null || missing+=(ffmpeg)
if [[ ${#missing[@]} -gt 0 ]]; then
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
fi
systemctl disable --now icecast2.service >/dev/null 2>&1 || true

# ---------------------------------------------------------------- user/dirs
log "Creating user and directories"
if ! id "$RADIO_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$RADIO_USER"
fi

install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/releases" "$APP_DIR/frontend-dist"
install -d -o "$RADIO_USER" -g "$RADIO_GROUP" -m 0755 "$MUSIC_DIR" "$COVER_DIR" "$STATE_DIR" "$LOG_DIR" "$LOG_DIR/icecast" "$BACKUP_DIR"
install -d -o root -g "$RADIO_GROUP" -m 0770 "$ETC_DIR"
# The backend rewrites encoder.liq / station.liq atomically, which needs a
# writable directory; an existing installation may still have 0750.
chmod 0770 "$ETC_DIR"
install -d -o "$RADIO_USER" -g "$RADIO_GROUP" -m 0750 "$RUN_DIR"
install -d -o root -g root -m 0755 /etc/caddy/sites

# ---------------------------------------------------------------- release
log "Fetching release $SHA"
REL="$APP_DIR/releases/$SHA"
if [[ ! -d "$REL/.git" ]]; then
  tmp="$(mktemp -d)"
  git clone --quiet --no-checkout "$REPO_URL" "$tmp/repo"
  git -C "$tmp/repo" fetch --quiet origin "$SHA" 2>/dev/null || true
  git -C "$tmp/repo" checkout --quiet "$SHA"
  mv "$tmp/repo" "$REL"
  rm -rf "$tmp"
fi
chown -R root:root "$REL"

log "Activating release"
ln -sfn "$REL" "$APP_DIR/current.tmp"
mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"

ls -1dt "$APP_DIR"/releases/* 2>/dev/null | tail -n +4 | while read -r old; do
  if [[ "$(readlink -f "$APP_DIR/current")" != "$(readlink -f "$old")" ]]; then
    rm -rf "$old"
  fi
done

# ---------------------------------------------------------------- python env
log "Python virtual environment"
if [[ ! -x "$APP_DIR/venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/venv"
fi
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip wheel
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/current/backend/requirements.txt"
chown -R root:root "$APP_DIR/venv"

# ---------------------------------------------------------------- secrets
log "Configuration and secrets"
gen() { openssl rand -hex "${1:-24}"; }

if [[ ! -f "$ETC_DIR/env" ]]; then
  cat > "$ETC_DIR/env" <<EOF
VD_DB_PATH=$STATE_DIR/app.db
VD_MUSIC_DIR=$MUSIC_DIR
VD_COVER_DIR=$COVER_DIR
VD_ENGINE_DIR=$RUN_DIR
VD_ENGINE_QUEUE_FILE=$RUN_DIR/engine-queue.txt
VD_ENGINE_SOCKET=$RUN_DIR/liquidsoap.sock
VD_ENCODER_FILE=$ETC_DIR/encoder.liq
VD_STATION_FILE=$ETC_DIR/station.liq
VD_LOG_DIR=$LOG_DIR
VD_FRONTEND_DIST=$APP_DIR/frontend-dist
VD_ICECAST_STATS_URL=http://127.0.0.1:8101/status-json.xsl
VD_ICECAST_ADMIN_USER=vdadmin
VD_ICECAST_ADMIN_PASSWORD=$(gen 20)
VD_INTERNAL_TOKEN=$(gen 32)
VD_BACKEND_URL=http://127.0.0.1:8100
VD_PUBLIC_BASE_URL=https://vanilladream.duckdns.org
VD_SECURE_COOKIES=1
VD_SCHEDULER_TICK=1.0
EOF
fi
chown root:"$RADIO_GROUP" "$ETC_DIR/env"
chmod 0640 "$ETC_DIR/env"

set -a
# shellcheck disable=SC1090
source "$ETC_DIR/env"
set +a

if [[ ! -f "$ETC_DIR/icecast.xml" ]]; then
  source_pw="$(gen 20)"
  relay_pw="$(gen 20)"
  sed -e "s|__SOURCE_PASSWORD__|$source_pw|" \
      -e "s|__RELAY_PASSWORD__|$relay_pw|" \
      -e "s|__ADMIN_PASSWORD__|$VD_ICECAST_ADMIN_PASSWORD|" \
      "$APP_DIR/current/radio/icecast.xml.template" > "$ETC_DIR/icecast.xml"
  printf '%s\n' "$source_pw" > "$ETC_DIR/icecast-source-password"
fi
chown "$RADIO_USER":"$RADIO_GROUP" "$ETC_DIR/icecast.xml"
chmod 0640 "$ETC_DIR/icecast.xml"
chown root:"$RADIO_GROUP" "$ETC_DIR/icecast-source-password"
chmod 0640 "$ETC_DIR/icecast-source-password"

if [[ ! -f "$ETC_DIR/liquidsoap.env.liq" ]]; then
  cat > "$ETC_DIR/liquidsoap.env.liq" <<EOF
radio_runtime_dir = "$RUN_DIR"
radio_queue_file = "$RUN_DIR/engine-queue.txt"
radio_internal_url = "http://127.0.0.1:8100/api/internal/engine/track-start"
radio_internal_token = "$VD_INTERNAL_TOKEN"
radio_icecast_source_password = "$(cat "$ETC_DIR/icecast-source-password")"
radio_mount = "/radio.mp3"
radio_public_url = "$VD_PUBLIC_BASE_URL"
EOF
fi
chown root:"$RADIO_GROUP" "$ETC_DIR/liquidsoap.env.liq"
chmod 0640 "$ETC_DIR/liquidsoap.env.liq"

install -o root -g root -m 0644 "$APP_DIR/current/radio/liquidsoap.liq" "$ETC_DIR/liquidsoap.liq"

if [[ ! -f "$ETC_DIR/encoder.liq" ]]; then
  printf 'encoder_bitrate = 128\n' > "$ETC_DIR/encoder.liq"
fi
if [[ ! -f "$ETC_DIR/station.liq" ]]; then
  cat > "$ETC_DIR/station.liq" <<'EOF'
station_name = "Vanilla Dream Radio"
station_description = "Круглосуточный сиреневый эфир"
station_genre = "Eclectic"
EOF
fi
chown root:"$RADIO_GROUP" "$ETC_DIR/encoder.liq" "$ETC_DIR/station.liq"
chmod 0660 "$ETC_DIR/encoder.liq" "$ETC_DIR/station.liq"

# ---------------------------------------------------------------- config check
log "Validating Liquidsoap configuration"
if ! sudo -u "$RADIO_USER" /usr/bin/liquidsoap --check "$ETC_DIR/liquidsoap.liq"; then
  die "liquidsoap configuration is invalid — installation stopped"
fi

# ---------------------------------------------------------------- units
log "Installing systemd units, sudoers, logrotate, tmpfiles"
install -o root -g root -m 0644 "$APP_DIR/current/deploy/systemd/vanilla-dream-radio.slice" /etc/systemd/system/vanilla-dream-radio.slice
for unit in api liquidsoap icecast backup; do
  install -o root -g root -m 0644 "$APP_DIR/current/deploy/systemd/vanilla-dream-radio-$unit.service" "/etc/systemd/system/vanilla-dream-radio-$unit.service"
done
install -o root -g root -m 0644 "$APP_DIR/current/deploy/systemd/vanilla-dream-radio-backup.timer" /etc/systemd/system/vanilla-dream-radio-backup.timer
install -o root -g root -m 0440 "$APP_DIR/current/deploy/sudoers/vanilla-dream-radio" /etc/sudoers.d/vanilla-dream-radio
visudo -cf /etc/sudoers.d/vanilla-dream-radio >/dev/null
install -o root -g root -m 0644 "$APP_DIR/current/deploy/logrotate/vanilla-dream-radio" /etc/logrotate.d/vanilla-dream-radio
install -o root -g root -m 0644 "$APP_DIR/current/deploy/tmpfiles/vanilla-dream-radio.conf" /etc/tmpfiles.d/vanilla-dream-radio.conf
systemd-tmpfiles --create /etc/tmpfiles.d/vanilla-dream-radio.conf
systemctl daemon-reload

run_as_radio() {
  sudo -u "$RADIO_USER" bash -c "set -a; . '$ETC_DIR/env'; set +a; cd '$APP_DIR/current/backend' && exec \"\$@\"" -- "$@"
}

# ---------------------------------------------------------------- database
log "Database"
run_as_radio "$APP_DIR/venv/bin/python" -m app.cli init-db || die "database initialisation failed"

if [[ -n "$ADMIN_PASSWORD_FILE" && -f "$ADMIN_PASSWORD_FILE" ]]; then
  if run_as_radio "$APP_DIR/venv/bin/python" -m app.cli password-is-set 2>/dev/null | grep -q '^set$'; then
    log "Admin password already set — keeping the existing one"
  else
    log "Setting the admin password"
    run_as_radio "$APP_DIR/venv/bin/python" -m app.cli set-password < "$ADMIN_PASSWORD_FILE" \
      || die "could not set the admin password"
  fi
fi

# ---------------------------------------------------------------- services
log "Icecast"
systemctl enable --now vanilla-dream-radio-icecast.service
for _ in $(seq 1 20); do
  if curl -fsS --max-time 2 http://127.0.0.1:8101/status-json.xsl >/dev/null 2>&1; then break; fi
  sleep 0.5
done
curl -fsS --max-time 3 http://127.0.0.1:8101/status-json.xsl >/dev/null || die "icecast did not start"

log "API"
systemctl enable vanilla-dream-radio-api.service
systemctl restart vanilla-dream-radio-api.service
ok=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 http://127.0.0.1:8100/api/public/now-playing >/dev/null 2>&1; then ok=1; break; fi
  sleep 0.5
done
[[ $ok -eq 1 ]] || { journalctl -u vanilla-dream-radio-api.service -n 40 --no-pager; die "API did not start"; }

systemctl enable vanilla-dream-radio-backup.timer >/dev/null
systemctl start vanilla-dream-radio-backup.timer

# ---------------------------------------------------------------- caddy
log "Caddy virtual host"
install -o root -g root -m 0644 "$APP_DIR/current/deploy/caddy/vanilla-dream.caddy" /etc/caddy/sites/vanilla-dream.caddy
if ! grep -qE '^\s*import\s+/etc/caddy/sites/\*\.caddy' /etc/caddy/Caddyfile; then
  backup="/etc/caddy/Caddyfile.vdr-backup.$(date +%Y%m%d%H%M%S)"
  cp /etc/caddy/Caddyfile "$backup"
  printf '\n# Vanilla Dream Radio sites\nimport /etc/caddy/sites/*.caddy\n' >> /etc/caddy/Caddyfile
fi
if ! caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1; then
  [[ -n "${backup:-}" && -f "$backup" ]] && cp "$backup" /etc/caddy/Caddyfile
  die "caddy configuration is invalid — previous Caddyfile restored"
fi
systemctl reload caddy
sleep 2
curl -fsS --max-time 5 http://127.0.0.1:8000/healthz >/dev/null || die "Adminbot health check failed after caddy reload"

log "Installation finished"
cat <<EOF

  Release:   $(readlink -f "$APP_DIR/current")
  API:       http://127.0.0.1:8100/api/public/now-playing
  Icecast:   http://127.0.0.1:8101/status-json.xsl
  Music:     $MUSIC_DIR
  Database:  $STATE_DIR/app.db
  Config:    $ETC_DIR
  Adminbot:  $(curl -s --max-time 5 http://127.0.0.1:8000/healthz)

  The engine is deliberately not started: the station stays stopped until the
  owner starts it from the panel (desired_state=stopped).

EOF
