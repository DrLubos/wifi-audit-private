#!/usr/bin/env bash
#
# install_uploader.sh - install the wifi-sensor uploader (live upload, step 4;
# ../server/docs/live-upload.md)
#
# Copies the uploader (uploader/) to the install directory, creates its
# configuration from uploader/uploader.conf.example (never overwritten) and the
# systemd unit (systemd/wifi-sensor-uploader.service). Requires collector
# buffer v8 (install_collector.sh of live-upload step 3).
#
# The ingest token: the server's `ops/sensor_token.sh create <sensor> --stdout`
# piped over ssh into the sensor user's ~/.wifi-sensor-ingest-token (mode 0600),
# never printed. This installer moves it to /etc/wifi-sensor/ingest.token
# (root:<sensor user> 0640) - the home copy is removed - and keeps an existing
# token when there is no new one.
#
# Usage - on the Pi, from the wifi-sensor/ folder of the monorepo clone:
#
#   sudo ./install_uploader.sh                 # install / update; enables the
#                                              # service only when INGEST_URL and
#                                              # the token are set
#   sudo ./install_uploader.sh --no-enable     # install, leave it stopped
#
# Then: sudo -u <sensor user> python3 /opt/wifi-sensor/uploader/uploader.py --check
#
# Configuration (sensor.conf next to this script or environment): SENSOR_USER,
# INSTALL_DIR (/opt/wifi-sensor), INGEST_URL (written into a new uploader.conf).
#
# Idempotent; restarts the service when its code or configuration changed.
# Rollback: sudo systemctl disable --now wifi-sensor-uploader (the uploader
# never writes the buffer: the collector and its data are untouched).
#
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_VARS=(SENSOR_USER INSTALL_DIR INGEST_URL)
UNIT_NAME="wifi-sensor-uploader.service"
UNIT_TEMPLATE="$SCRIPT_DIR/systemd/$UNIT_NAME"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
ETC_DIR="/etc/wifi-sensor"
UPLOADER_CONF="$ETC_DIR/uploader.conf"
TOKEN_FILE="$ETC_DIR/ingest.token"
SRC_DIR="$SCRIPT_DIR/uploader"

ts()   { date '+%H:%M:%S'; }
log()  { printf '%s [info] %s\n' "$(ts)" "$*"; }
warn() { printf '%s [warn] %s\n' "$(ts)" "$*" >&2; }
die()  { printf '%s [fail] %s\n' "$(ts)" "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

CHANGED=0
write_if_changed() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then
    log "unchanged: $dest"
  else
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    log "wrote: $dest"
    CHANGED=1
  fi
  chmod "$mode" "$dest"
  chown "$owner" "$dest"
  rm -f "$tmp"
}

load_config() {
  local v conf="${SENSOR_CONF:-$SCRIPT_DIR/sensor.conf}"
  local -A from_env=()
  [[ -f $conf ]] || return 0
  for v in "${CONF_VARS[@]}"; do
    if [[ -n ${!v:-} ]]; then from_env[$v]=${!v}; fi
  done
  # shellcheck disable=SC1090
  source "$conf"
  for v in "${!from_env[@]}"; do printf -v "$v" '%s' "${from_env[$v]}"; done
}

conf_value() { sed -n "s/^$1=//p" "$UPLOADER_CONF" 2>/dev/null | tail -1; }

main() {
  local enable=1
  [[ ${1:-} == --no-enable ]] && enable=0
  step "Preflight"
  [[ $EUID -eq 0 ]] || die "run with sudo from your normal user account: sudo $0"
  load_config
  SENSOR_USER="${SENSOR_USER:-${SUDO_USER:-}}"
  [[ -n $SENSOR_USER && $SENSOR_USER != root ]] || die "cannot determine the sensor user; set SENSOR_USER"
  SENSOR_HOME=$(getent passwd "$SENSOR_USER" | cut -d: -f6)
  INSTALL_DIR="${INSTALL_DIR:-/opt/wifi-sensor}"
  [[ -f $SRC_DIR/uploader.py ]] || die "uploader sources not found in $SRC_DIR"
  [[ -f $INSTALL_DIR/collector/clock.py ]] || die "collector v8 not installed (clock.py missing) - run install_collector.sh first"
  local v
  v=$(sudo -u "$SENSOR_USER" python3 -c "import sqlite3; db = sqlite3.connect('file:/var/lib/wifi-sensor/buffer.db?mode=ro', uri=True); print(db.execute(\"SELECT value FROM meta WHERE key = 'schema_version'\").fetchone()[0])" 2>/dev/null || echo "?")
  [[ $v =~ ^[0-9]+$ ]] && (( v >= 8 )) \
    || die "the buffer is schema '$v' - the uploader needs v8 or newer (install_collector.sh, live-upload step 3)"
  log "sensor user: $SENSOR_USER  install: $INSTALL_DIR  buffer: v$v"

  step "Uploader code -> $INSTALL_DIR/uploader"
  install -d -m 0755 -o root -g root "$INSTALL_DIR/uploader"
  local f
  # capture_config.py (sensor/): the configuration rules and hash the uploader
  # checks a delivered configuration with (server/docs/config-delivery.md 4.3)
  for f in "$SRC_DIR"/*.py "$SRC_DIR"/../sensor/capture_config.py "$SRC_DIR"/README.md "$SRC_DIR"/uploader.conf.example; do
    [[ -f $f ]] || continue
    write_if_changed "$INSTALL_DIR/uploader/$(basename "$f")" 0644 root:root <"$f"
  done
  rm -rf "$INSTALL_DIR/uploader/__pycache__"

  step "Configuration"
  install -d -m 0755 -o root -g root "$ETC_DIR"
  if [[ -f $UPLOADER_CONF ]]; then
    log "keeping existing $UPLOADER_CONF"
  else
    write_if_changed "$UPLOADER_CONF" 0644 root:root \
      < <(sed -e "s|^INGEST_URL=.*|INGEST_URL=${INGEST_URL:-}|" "$SRC_DIR/uploader.conf.example")
  fi
  local home_token="$SENSOR_HOME/.wifi-sensor-ingest-token"
  if [[ -s $home_token ]]; then
    # the token goes from the sensor user's home to /etc, never through a terminal
    install -m 0640 -o root -g "$SENSOR_USER" "$home_token" "$TOKEN_FILE"
    rm -f "$home_token"
    log "ingest token installed: $TOKEN_FILE (root:$SENSOR_USER 0640); the home copy removed"
    CHANGED=1
  elif [[ -s $TOKEN_FILE ]]; then
    log "keeping the existing ingest token ($TOKEN_FILE)"
  else
    warn "no ingest token yet ($home_token or $TOKEN_FILE)"
  fi

  step "systemd: $UNIT_NAME"
  write_if_changed "$UNIT_DEST" 0644 root:root \
    < <(sed -e "s|__SENSOR_USER__|$SENSOR_USER|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$UNIT_TEMPLATE")
  systemctl daemon-reload
  if [[ -z $(conf_value INGEST_URL) || ! -s $TOKEN_FILE ]]; then
    warn "INGEST_URL or the token missing - the service is installed but not enabled"
    enable=0
  fi
  if [[ $enable -eq 0 ]]; then
    log "not enabled. Check: sudo -u $SENSOR_USER python3 $INSTALL_DIR/uploader/uploader.py --check"
    log "then: sudo systemctl enable --now $UNIT_NAME"
    return 0
  fi
  systemctl enable "$UNIT_NAME"
  if [[ $CHANGED -eq 1 ]] || ! systemctl is-active --quiet "$UNIT_NAME"; then
    systemctl restart "$UNIT_NAME"
  fi
  sleep 5
  systemctl is-active --quiet "$UNIT_NAME" || { journalctl -u "$UNIT_NAME" -n 30 --no-pager; die "$UNIT_NAME is not running"; }
  journalctl -u "$UNIT_NAME" -n 8 --no-pager -o cat || true
  log "running. Watch: journalctl -u $UNIT_NAME -f"
}

main "$@"
