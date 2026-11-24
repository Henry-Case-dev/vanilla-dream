#!/usr/bin/env bash
# Vanilla Dream Radio — deploy a new release of an existing installation.
#
# Usage: sudo bash deploy/update.sh --sha <git-sha> [--frontend <tar.gz>]
#
# Keeps database, music, covers and settings. Rolls the symlink back when the
# API does not come up healthy.
set -Eeuo pipefail

APP_DIR=/opt/vanilla-dream-radio
ETC_DIR=/etc/vanilla-dream-radio
RADIO_USER=vdradio
REPO_URL="${VDR_REPO_URL:-https://github.com/Henry-Case-dev/vanilla-dream.git}"

SHA=""
FRONTEND=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --sha) SHA="${2:-}"; shift 2 ;;
    --frontend) FRONTEND="${2:-}"; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

log() { printf '\n== %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root"
[[ -n "$SHA" ]] || die "--sha is required"

PREV="$(readlink -f "$APP_DIR/current" || true)"
REL="$APP_DIR/releases/$SHA"

log "Fetching $SHA"
if [[ ! -d "$REL/.git" ]]; then
  tmp="$(mktemp -d)"
  git clone --quiet --no-checkout "$REPO_URL" "$tmp/repo"
  git -C "$tmp/repo" fetch --quiet origin "$SHA" 2>/dev/null || true
  git -C "$tmp/repo" checkout --quiet "$SHA"
  mv "$tmp/repo" "$REL"
  rm -rf "$tmp"
fi
chown -R root:root "$REL"

log "Validating Liquidsoap configuration"
install -o root -g root -m 0644 "$REL/radio/liquidsoap.liq" "$ETC_DIR/liquidsoap.liq"
sudo -u "$RADIO_USER" /usr/bin/liquidsoap --check "$ETC_DIR/liquidsoap.liq" || die "invalid liquidsoap configuration"

log "Installing Python dependencies"
"$APP_DIR/venv/bin/pip" install --quiet -r "$REL/backend/requirements.txt"

log "Switching release"
ln -sfn "$REL" "$APP_DIR/current.tmp"
mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"

if [[ -n "$FRONTEND" && -f "$FRONTEND" ]]; then
  log "Updating frontend build"
  stage="$(mktemp -d)"
  tar -xzf "$FRONTEND" -C "$stage"
  if [[ -d "$stage/dist" ]]; then src="$stage/dist"; else src="$stage"; fi
  rsync -a --delete "$src"/ "$APP_DIR/frontend-dist"/
  chown -R root:root "$APP_DIR/frontend-dist"
  rm -rf "$stage"
fi

log "Restarting API"
systemctl restart vanilla-dream-radio-api.service
ok=0
for _ in $(seq 1 40); do
  if curl -fsS --max-time 2 http://127.0.0.1:8100/api/public/now-playing >/dev/null 2>&1; then ok=1; break; fi
  sleep 0.5
done
if [[ $ok -ne 1 ]]; then
  journalctl -u vanilla-dream-radio-api.service -n 40 --no-pager || true
  if [[ -n "$PREV" && -d "$PREV" ]]; then
    log "Rolling back to $PREV"
    ln -sfn "$PREV" "$APP_DIR/current.tmp"
    mv -Tf "$APP_DIR/current.tmp" "$APP_DIR/current"
    systemctl restart vanilla-dream-radio-api.service || true
  fi
  die "new release is not healthy — previous release restored"
fi

# The engine only restarts when it was running; a stopped station stays stopped.
if systemctl is-active --quiet vanilla-dream-radio-liquidsoap.service; then
  log "Restarting the engine to pick up the new script"
  systemctl restart vanilla-dream-radio-liquidsoap.service
fi

if [[ -n "$FRONTEND" ]]; then systemctl reload caddy || true; fi

# keep the three newest releases
ls -1dt "$APP_DIR"/releases/* 2>/dev/null | tail -n +4 | while read -r old; do
  if [[ "$(readlink -f "$APP_DIR/current")" != "$(readlink -f "$old")" ]]; then
    rm -rf "$old"
  fi
done

log "Deployed $SHA"
printf '  current -> %s\n  adminbot: %s\n' "$(readlink -f "$APP_DIR/current")" "$(curl -s --max-time 5 http://127.0.0.1:8000/healthz)"
