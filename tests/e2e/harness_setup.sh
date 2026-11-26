#!/usr/bin/env bash
# Create an isolated Vanilla Dream Radio instance for end-to-end checks.
# Test-only: separate DB, music, covers, Icecast (8401), API (8301), Liquidsoap
# socket, its own systemd unit and its own front door (8300). Production units,
# ports, database and music are never touched.
set -Eeuo pipefail

ROOT=/srv/vdr-e2e
APP="${VDR_E2E_APP:-/opt/vanilla-dream-radio/current}"
DIST="${VDR_E2E_DIST:-/opt/vanilla-dream-radio/frontend-dist}"
PORT_API=8301
PORT_ICE=8401
PORT_WEB=8300
UNIT=vanilla-dream-radio-e2e-liquidsoap.service

log() { printf '\n== %s\n' "$*"; }

log "Reset isolated state (test instance only)"
pkill -f "uvicorn app.main:app --host 127.0.0.1 --port $PORT_API" 2>/dev/null || true
systemctl stop "$UNIT" vanilla-dream-radio-e2e-icecast.service 2>/dev/null || true
rm -rf "$ROOT"
install -d -o vdradio -g vdradio -m 0755 "$ROOT"/{music,covers,covers/playlists,state,log,run,etc}
install -d -m 0755 "$ROOT/fixtures"

# Short distinguishable tones: twelve seconds each, one frequency per track, so
# a real playback sequence can be verified by ear and by the stream metadata
# while manual queue edits still have time to be observed.
log "Test tones"
frequencies=(220 262 330 392 440 523 659 784)
index=0
for frequency in "${frequencies[@]}"; do
  index=$((index + 1))
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$frequency:duration=12" \
    -ac 2 -ar 44100 -b:a 128k \
    -metadata title="Tone $index" -metadata artist="E2E" -metadata album="Core03" \
    "$ROOT/fixtures/tone-$index.mp3"
done

# Replacement versions: same tags, a different frequency, bitrate and duration so
# an actual file switch is observable by duration/bitrate and by ear.
for index in 1 2 3 4; do
  frequency=$((frequencies[index - 1] + 55))
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$frequency:duration=18" \
    -ac 2 -ar 44100 -b:a 96k \
    -metadata title="Tone $index" -metadata artist="E2E" -metadata album="Core03" \
    "$ROOT/fixtures/tone-$index-v2.mp3"
done
for index in 1 2; do
  frequency=$((frequencies[index - 1] + 110))
  ffmpeg -loglevel error -y -f lavfi -i "sine=frequency=$frequency:duration=20" \
    -ac 2 -ar 44100 -b:a 112k \
    -metadata title="Tone $index" -metadata artist="E2E" -metadata album="Core03" \
    "$ROOT/fixtures/tone-$index-v3.mp3"
done
ls "$ROOT/fixtures"

log "Icecast config"
cat > "$ROOT/etc/icecast.xml" <<EOF
<icecast>
  <location>VDR e2e</location>
  <admin>e2e@localhost</admin>
  <limits><clients>50</clients><sources>2</sources><queue-size>524288</queue-size>
    <client-timeout>30</client-timeout><header-timeout>15</header-timeout><source-timeout>30</source-timeout>
    <burst-on-connect>1</burst-on-connect><burst-size>65536</burst-size></limits>
  <authentication>
    <source-password>e2esource</source-password><relay-password>e2erelay</relay-password>
    <admin-user>admin</admin-user><admin-password>e2eadmin</admin-password>
  </authentication>
  <hostname>localhost</hostname>
  <listen-socket><port>$PORT_ICE</port><bind-address>127.0.0.1</bind-address></listen-socket>
  <paths>
    <basedir>/usr/share/icecast2</basedir><logdir>$ROOT/log</logdir>
    <webroot>/usr/share/icecast2/web</webroot><adminroot>/usr/share/icecast2/admin</adminroot>
    <pidfile>$ROOT/run/icecast.pid</pidfile><alias source="/" destination="/status.xsl"/>
  </paths>
  <logging><accesslog>access.log</accesslog><errorlog>error.log</errorlog><loglevel>3</loglevel></logging>
  <security><chroot>0</chroot></security>
</icecast>
EOF

log "Liquidsoap config"
cat > "$ROOT/etc/liquidsoap.env.liq" <<EOF
radio_runtime_dir = "$ROOT/run"
radio_queue_file = "$ROOT/run/engine-queue.txt"
radio_internal_url = "http://127.0.0.1:$PORT_API/api/internal/engine/track-start"
radio_internal_token = "e2e-token"
radio_icecast_source_password = "e2esource"
radio_mount = "/radio.mp3"
radio_public_url = "http://127.0.0.1:18080"
EOF
printf 'encoder_bitrate = 128\n' > "$ROOT/etc/encoder.liq"
cat > "$ROOT/etc/station.liq" <<'EOF'
station_name = "VDR e2e"
station_description = "Изолированная проверка"
station_genre = "Test"
EOF
sed -e "s|/etc/vanilla-dream-radio|$ROOT/etc|g" -e "s|port=8101|port=$PORT_ICE|" "$APP/radio/liquidsoap.liq" > "$ROOT/etc/liquidsoap.liq"
chown -R vdradio:vdradio "$ROOT"
chmod 0644 "$ROOT/etc/liquidsoap.liq"

log "Isolated units"
cat > /etc/systemd/system/vanilla-dream-radio-e2e-icecast.service <<EOF
[Unit]
Description=VDR e2e Icecast
After=network.target
[Service]
Type=simple
User=vdradio
Group=vdradio
ExecStart=/usr/bin/icecast2 -c $ROOT/etc/icecast.xml
Restart=no
[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/$UNIT <<EOF
[Unit]
Description=VDR e2e Liquidsoap
After=network.target vanilla-dream-radio-e2e-icecast.service
[Service]
Type=simple
User=vdradio
Group=vdradio
ExecStartPre=/usr/bin/liquidsoap --check $ROOT/etc/liquidsoap.liq
ExecStart=/usr/bin/liquidsoap $ROOT/etc/liquidsoap.liq
Restart=no
[Install]
WantedBy=multi-user.target
EOF

log "Isolated sudoers (test unit only)"
cat > /etc/sudoers.d/vdr-e2e <<EOF
vdradio ALL=(root) NOPASSWD: /usr/bin/systemctl start $UNIT
vdradio ALL=(root) NOPASSWD: /usr/bin/systemctl stop $UNIT
vdradio ALL=(root) NOPASSWD: /usr/bin/systemctl restart $UNIT
vdradio ALL=(root) NOPASSWD: /usr/bin/systemctl is-active $UNIT
EOF
chmod 0440 /etc/sudoers.d/vdr-e2e
visudo -cf /etc/sudoers.d/vdr-e2e >/dev/null

systemctl daemon-reload
systemctl start vanilla-dream-radio-e2e-icecast.service
sleep 2
curl -fsS --max-time 3 http://127.0.0.1:$PORT_ICE/status-json.xsl >/dev/null && echo "e2e icecast up on $PORT_ICE"

log "Isolated API as vdradio (same user as the engine, like production)"
cat > "$ROOT/state/env" <<EOF
VD_DB_PATH=$ROOT/state/app.db
VD_MUSIC_DIR=$ROOT/music
VD_COVER_DIR=$ROOT/covers
VD_PLAYLIST_COVER_DIR=$ROOT/covers/playlists
VD_LOG_DIR=$ROOT/log
VD_ENGINE_DIR=$ROOT/run
VD_ENGINE_QUEUE_FILE=$ROOT/run/engine-queue.txt
VD_ENGINE_SOCKET=$ROOT/run/liquidsoap.sock
VD_ENGINE_UNIT=$UNIT
VD_STATION_SUDO=1
VD_ENGINE_LABEL=Liquidsoap-e2e
VD_ENCODER_FILE=$ROOT/etc/encoder.liq
VD_STATION_FILE=$ROOT/etc/station.liq
VD_ICECAST_STATS_URL=http://127.0.0.1:$PORT_ICE/status-json.xsl
VD_ICECAST_ADMIN_USER=admin
VD_ICECAST_ADMIN_PASSWORD=e2eadmin
VD_INTERNAL_TOKEN=e2e-token
VD_BACKEND_URL=http://127.0.0.1:$PORT_API
VD_PUBLIC_BASE_URL=http://127.0.0.1:18080
VD_SECURE_COOKIES=0
VD_SCHEDULER_TICK=1.0
EOF
chown root:vdradio "$ROOT/state/env"
chmod 0640 "$ROOT/state/env"

cd "$APP/backend"
sudo -u vdradio env $(grep -v '^#' "$ROOT/state/env" | xargs) \
  /opt/vanilla-dream-radio/venv/bin/python -m app.cli init-db
printf 'e2e-owner-password\n' | sudo -u vdradio env $(grep -v '^#' "$ROOT/state/env" | xargs) \
  /opt/vanilla-dream-radio/venv/bin/python -m app.cli set-password

setsid sudo -u vdradio env $(grep -v '^#' "$ROOT/state/env" | xargs) \
  /opt/vanilla-dream-radio/venv/bin/uvicorn app.main:app --host 127.0.0.1 --port $PORT_API \
  > "$ROOT/log/api.log" 2>&1 < /dev/null &
sleep 3
curl -fsS --max-time 5 http://127.0.0.1:$PORT_API/api/public/now-playing >/dev/null && echo "e2e api up on $PORT_API"

log "Front door (static + proxy)"
pkill -f harness_proxy.py 2>/dev/null || true
sleep 1
cd "$APP"
nohup setsid env VDR_E2E_DIST="$DIST" \
  VDR_E2E_API_PORT=$PORT_API VDR_E2E_ICECAST_PORT=$PORT_ICE VDR_E2E_PROXY_PORT=$PORT_WEB \
  python3 tests/e2e/harness_proxy.py > "$ROOT/log/proxy.log" 2>&1 < /dev/null &
sleep 2
curl -fsS --max-time 5 -o /dev/null -w "e2e front door: %{http_code}\n" http://127.0.0.1:$PORT_WEB/

log "Tunnel: ssh -N -L 18080:127.0.0.1:8300 root@192.255.209.205"
echo "Browser URL: http://127.0.0.1:18080/"
