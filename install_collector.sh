#!/usr/bin/env bash
#
# install_collector.sh - install the wifi-sensor collector (Milestone 2, part 1)
#
# Copies the collector (collector/) and the one-off analysis scripts (analysis/)
# from this repository to the Pi's install directory, writes the collector's
# configuration, and runs the collector as an unprivileged systemd service that
# starts after Kismet (unit template: systemd/wifi-sensor-collector.service).
# Requires install_sensor.sh to have been run first (Kismet installed, sensor
# user in group kismet, credentials in ~/.kismet/kismet_httpd.conf).
#
# Usage - on the Pi, from your normal user account:
#
#   sudo ./install_collector.sh
#
# Configuration - environment variables, or KEY=value lines in sensor.conf
# next to this script (shared with install_sensor.sh):
#
#   SENSOR_USER   Unprivileged user that runs the collector (and Kismet).
#                 Default: the user who invoked sudo.
#   INSTALL_DIR   Where the code is copied (collector/ and analysis/ subdirs).
#                 Default: /opt/wifi-sensor
#   DATA_DIR      SQLite buffer directory.              Default: /var/lib/wifi-sensor
#   SENSOR_CONF   Config file path. Default: <script dir>/sensor.conf
#
# The runtime configuration of the collector itself lives in
# /etc/wifi-sensor/collector.conf (created from collector/collector.conf.example
# on first run, never overwritten afterwards).
#
# Idempotent: re-run after every code change; the service is restarted only
# when something changed.
#
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export DEBIAN_FRONTEND=noninteractive

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_VARS=(SENSOR_USER INSTALL_DIR DATA_DIR)

UNIT_NAME="wifi-sensor-collector.service"
UNIT_TEMPLATE="$SCRIPT_DIR/systemd/$UNIT_NAME"
UNIT_DEST="/etc/systemd/system/$UNIT_NAME"
ETC_DIR="/etc/wifi-sensor"
COLLECTOR_CONF="$ETC_DIR/collector.conf"
SRC_DIR="$SCRIPT_DIR/collector"
ANALYSIS_SRC_DIR="$SCRIPT_DIR/analysis"

# ------------------------------------------------------------------ helpers --
ts()   { date '+%H:%M:%S'; }
log()  { printf '%s [info] %s\n' "$(ts)" "$*"; }
warn() { printf '%s [warn] %s\n' "$(ts)" "$*" >&2; }
die()  { printf '%s [fail] %s\n' "$(ts)" "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
trap 'die "aborted at line $LINENO: $BASH_COMMAND"' ERR

usage() {
  sed -n '3,/^set -Eeuo/{ /^set -Eeuo/d; s/^# \{0,1\}//p }' "${BASH_SOURCE[0]}"
}

CHANGED=0
mark_changed() { CHANGED=1; }

# write_if_changed DEST MODE OWNER:GROUP   (content on stdin)
write_if_changed() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then
    log "unchanged: $dest"
  else
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    log "wrote: $dest"
    mark_changed
  fi
  chmod "$mode" "$dest"
  chown "$owner" "$dest"
  rm -f "$tmp"
}

pkg_installed() {
  local status
  status=$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null || true)
  [[ $status == *"install ok installed"* ]]
}

load_config() {
  local v
  local -A from_env=()
  [[ -f $SENSOR_CONF ]] || return 0
  log "loading $SENSOR_CONF (environment variables take precedence)"
  for v in "${CONF_VARS[@]}"; do
    if [[ -n ${!v:-} ]]; then from_env[$v]=${!v}; fi
  done
  # shellcheck disable=SC1090
  source "$SENSOR_CONF"
  for v in "${!from_env[@]}"; do
    printf -v "$v" '%s' "${from_env[$v]}"
  done
}

# ---------------------------------------------------------------- steps --
check_prerequisites() {
  step "Prerequisites"
  [[ -d $SRC_DIR && -f $SRC_DIR/collector.py ]] || die "collector sources not found in $SRC_DIR"
  [[ -d $ANALYSIS_SRC_DIR ]] || die "analysis scripts not found in $ANALYSIS_SRC_DIR"
  [[ -f $UNIT_TEMPLATE ]] || die "unit template not found: $UNIT_TEMPLATE"
  [[ -f /etc/systemd/system/kismet.service || -f /lib/systemd/system/kismet.service \
     || -f /usr/lib/systemd/system/kismet.service ]] \
    || die "kismet.service not installed - run install_sensor.sh first"
  if ! id -nG "$SENSOR_USER" | tr ' ' '\n' | grep -qx kismet; then
    die "$SENSOR_USER is not in group kismet - run install_sensor.sh first"
  fi
  if ! pkg_installed python3 || ! pkg_installed python3-requests; then
    log "installing python3 and python3-requests"
    apt-get update -q
    apt-get install -y -q python3 python3-requests
  fi
  log "python: $(python3 --version)  requests: $(python3 -c 'import requests; print(requests.__version__)')"
  if [[ ! -s $AUTH_FILE ]]; then
    warn "$AUTH_FILE not found - the collector reads Kismet credentials from it;"
    warn "run install_sensor.sh, or set KISMET_USER/KISMET_PASS in $COLLECTOR_CONF"
  fi
}

install_code() {
  step "Collector code -> $INSTALL_DIR/collector"
  local f
  install -d -m 0755 -o root -g root "$INSTALL_DIR" "$INSTALL_DIR/collector"
  for f in "$SRC_DIR"/*.py "$SRC_DIR"/README.md "$SRC_DIR"/collector.conf.example; do
    [[ -f $f ]] || continue
    write_if_changed "$INSTALL_DIR/collector/$(basename "$f")" 0644 root:root <"$f"
  done
  # Byte-compiled files and stale modules from older versions are not wanted.
  rm -rf "$INSTALL_DIR/collector/__pycache__"
}

install_analysis() {
  step "Analysis scripts -> $INSTALL_DIR/analysis"
  # One-off, read-only scripts run by hand against the buffer. They are not part
  # of the service, so a change here must not restart it: the CHANGED flag is
  # preserved across this step.
  local f changed_before=$CHANGED
  install -d -m 0755 -o root -g root "$INSTALL_DIR/analysis"
  for f in "$ANALYSIS_SRC_DIR"/*.py "$ANALYSIS_SRC_DIR"/README.md; do
    [[ -f $f ]] || continue
    write_if_changed "$INSTALL_DIR/analysis/$(basename "$f")" 0644 root:root <"$f"
  done
  CHANGED=$changed_before
}

install_config() {
  step "Configuration"
  install -d -m 0755 -o root -g root "$ETC_DIR"
  if [[ -f $COLLECTOR_CONF ]]; then
    log "keeping existing $COLLECTOR_CONF"
  else
    # Redirection, not a pipe: a pipe would run write_if_changed in a subshell
    # and lose the CHANGED flag.
    write_if_changed "$COLLECTOR_CONF" 0644 root:root \
      < <(sed -e "s|^DB_PATH=.*|DB_PATH=$DATA_DIR/buffer.db|" "$SRC_DIR/collector.conf.example")
  fi
  if grep -Eq '^KISMET_PASS=' "$COLLECTOR_CONF"; then
    chmod 0640 "$COLLECTOR_CONF"
    chown "root:$SENSOR_USER" "$COLLECTOR_CONF"
    warn "$COLLECTOR_CONF contains KISMET_PASS - restricted to root:$SENSOR_USER 0640"
  fi
  install -d -m 0750 -o "$SENSOR_USER" -g kismet "$DATA_DIR"
  log "buffer directory: $DATA_DIR (owner $SENSOR_USER:kismet)"
}

install_service() {
  step "systemd: $UNIT_NAME"
  local rendered
  rendered=$(sed -e "s|__SENSOR_USER__|$SENSOR_USER|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" \
             "$UNIT_TEMPLATE")
  if [[ $DATA_DIR != /var/lib/wifi-sensor ]]; then
    rendered=${rendered//ReadWritePaths=\/var\/lib\/wifi-sensor/ReadWritePaths=$DATA_DIR}
  fi
  write_if_changed "$UNIT_DEST" 0644 root:root <<<"$rendered"
  systemctl daemon-reload
  if [[ $(systemctl is-enabled "$UNIT_NAME" 2>/dev/null || true) == enabled ]]; then
    log "$UNIT_NAME already enabled"
  else
    systemctl enable "$UNIT_NAME"
    log "$UNIT_NAME enabled on boot"
  fi
  if [[ $CHANGED -eq 1 ]] || ! systemctl is-active --quiet "$UNIT_NAME"; then
    log "(re)starting $UNIT_NAME"
    systemctl restart "$UNIT_NAME"
  else
    log "$UNIT_NAME already running with the current code and configuration"
  fi
}

verify() {
  step "Verifying"
  local i
  for ((i = 0; i < 20; i++)); do
    if journalctl -u "$UNIT_NAME" --since "-2 minutes" --no-pager -o cat 2>/dev/null \
         | grep -q "poll ok"; then
      break
    fi
    sleep 3
  done
  if ! systemctl is-active --quiet "$UNIT_NAME"; then
    journalctl -u "$UNIT_NAME" -n 30 --no-pager || true
    die "$UNIT_NAME is not running - see the journal above"
  fi
  journalctl -u "$UNIT_NAME" -n 5 --no-pager -o cat || true
  cat <<EOF

==> Done. Next steps
  Service    systemctl status $UNIT_NAME
             journalctl -u $UNIT_NAME -f
  Config     $COLLECTOR_CONF   (edit, then: sudo systemctl restart $UNIT_NAME)
  Buffer     $DATA_DIR/buffer.db
             sqlite3 -readonly $DATA_DIR/buffer.db 'SELECT * FROM polls ORDER BY id DESC LIMIT 5'
  Docs       $INSTALL_DIR/collector/README.md
  Analysis   python3 $INSTALL_DIR/analysis/analyze.py     (see $INSTALL_DIR/analysis/README.md)
  Re-run     sudo $0     (idempotent; copies changed code and restarts the service)
EOF
}

# -------------------------------------------------------------------- main --
main() {
  if [[ ${1:-} == -h || ${1:-} == --help ]]; then
    usage
    exit 0
  fi
  step "Preflight"
  [[ $EUID -eq 0 ]] || die "run with sudo from your normal user account: sudo $0"
  SENSOR_CONF="${SENSOR_CONF:-$SCRIPT_DIR/sensor.conf}"
  load_config
  SENSOR_USER="${SENSOR_USER:-${SUDO_USER:-}}"
  if [[ -z $SENSOR_USER || $SENSOR_USER == root ]]; then
    die "cannot determine the unprivileged user; run via sudo from your normal account or set SENSOR_USER"
  fi
  SENSOR_HOME=$(getent passwd "$SENSOR_USER" | cut -d: -f6 || true)
  [[ -n $SENSOR_HOME ]] || die "user '$SENSOR_USER' does not exist"
  AUTH_FILE="$SENSOR_HOME/.kismet/kismet_httpd.conf"
  INSTALL_DIR="${INSTALL_DIR:-/opt/wifi-sensor}"
  DATA_DIR="${DATA_DIR:-/var/lib/wifi-sensor}"
  log "sensor user: $SENSOR_USER  install: $INSTALL_DIR  data: $DATA_DIR"

  check_prerequisites
  install_code
  install_analysis
  install_config
  install_service
  verify
}

main "$@"
