#!/usr/bin/env bash
#
# install_config_apply.sh - install the capture-configuration applier
# (configuration delivery, ../server/docs/config-delivery.md 5)
#
# Copies sensor/config_apply.py and sensor/capture_config.py to
# $INSTALL_DIR/config-apply, writes /etc/wifi-sensor/config-apply.conf when
# missing (KISMET_AUTH_FILE = the sensor user's kismet_httpd.conf), and
# installs and enables the units:
#   wifi-sensor-config-apply.path / .service   apply a delivered configuration
#   wifi-sensor-config-recover.service         at boot, undo an interrupted apply
#   wifi-sensor-config-capabilities.timer      hourly: the adapter's channel list
# Kismet, the collector and the capture are NOT restarted or changed: the
# applier only acts when the uploader (CONFIG_APPLY=1 in uploader.conf) hands
# it a configuration - with CONFIG_APPLY=0 (the default) it never runs.
#
# Usage - on the Pi, from the wifi-sensor/ folder of the monorepo clone:
#
#   sudo ./install_config_apply.sh
#
# Idempotent; never deletes anything. Rollback: sudo systemctl disable --now
# wifi-sensor-config-apply.path wifi-sensor-config-capabilities.timer &&
# sudo systemctl disable wifi-sensor-config-recover.service
#
set -Eeuo pipefail
umask 022
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONF_VARS=(SENSOR_USER INSTALL_DIR)
ETC_DIR="/etc/wifi-sensor"
APPLY_CONF="$ETC_DIR/config-apply.conf"
UNITS=(wifi-sensor-config-apply.path wifi-sensor-config-apply.service wifi-sensor-config-recover.service
       wifi-sensor-config-capabilities.service wifi-sensor-config-capabilities.timer)

ts()   { date '+%H:%M:%S'; }
log()  { printf '%s [info] %s\n' "$(ts)" "$*"; }
warn() { printf '%s [warn] %s\n' "$(ts)" "$*" >&2; }
die()  { printf '%s [fail] %s\n' "$(ts)" "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }

write_if_changed() {
  local dest=$1 mode=$2 owner=$3 tmp
  tmp=$(mktemp)
  cat >"$tmp"
  if [[ -f $dest ]] && cmp -s "$tmp" "$dest"; then
    log "unchanged: $dest"
  else
    install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    log "wrote: $dest"
  fi
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

main() {
  step "Preflight"
  [[ $EUID -eq 0 ]] || die "run with sudo from your normal user account: sudo $0"
  load_config
  SENSOR_USER="${SENSOR_USER:-${SUDO_USER:-}}"
  [[ -n $SENSOR_USER && $SENSOR_USER != root ]] || die "cannot determine the sensor user; set SENSOR_USER"
  SENSOR_HOME=$(getent passwd "$SENSOR_USER" | cut -d: -f6)
  INSTALL_DIR="${INSTALL_DIR:-/opt/wifi-sensor}"
  [[ -f $SCRIPT_DIR/sensor/config_apply.py && -f $SCRIPT_DIR/sensor/capture_config.py ]] \
    || die "sources not found in $SCRIPT_DIR/sensor"
  [[ -f $SENSOR_HOME/.kismet/kismet_httpd.conf ]] || warn "no $SENSOR_HOME/.kismet/kismet_httpd.conf yet"
  log "sensor user: $SENSOR_USER  install: $INSTALL_DIR/config-apply"

  step "Applier code"
  local f
  for f in config_apply.py capture_config.py; do
    write_if_changed "$INSTALL_DIR/config-apply/$f" 0644 root:root <"$SCRIPT_DIR/sensor/$f"
  done
  rm -rf "$INSTALL_DIR/config-apply/__pycache__"

  step "Configuration"
  if [[ -f $APPLY_CONF ]]; then
    log "keeping existing $APPLY_CONF"
  else
    write_if_changed "$APPLY_CONF" 0644 root:root <<CONF
# The capture-configuration applier (config_apply.py; ../server/docs/config-delivery.md).
# It runs only when the uploader hands it a configuration (CONFIG_APPLY=1 in uploader.conf).
KISMET_AUTH_FILE=$SENSOR_HOME/.kismet/kismet_httpd.conf
#VERIFY_S=180
#ROLLBACK_VERIFY_S=120
CONF
  fi

  step "systemd"
  local u
  for u in "${UNITS[@]}"; do
    write_if_changed "/etc/systemd/system/$u" 0644 root:root \
      < <(sed -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$SCRIPT_DIR/systemd/$u")
  done
  systemctl daemon-reload
  systemctl enable wifi-sensor-config-recover.service
  systemctl enable --now wifi-sensor-config-apply.path wifi-sensor-config-capabilities.timer
  systemctl start wifi-sensor-config-capabilities.service \
    || warn "capabilities not recorded (journalctl -u wifi-sensor-config-capabilities)"
  [[ -s /var/lib/wifi-sensor-config/capabilities.json ]] \
    && log "capabilities: $(python3 -c 'import json; c = json.load(open("/var/lib/wifi-sensor-config/capabilities.json")); print(len(c.get("channels_supported") or []), "channels,", c.get("reg_country"))')"
  local accepting
  accepting=$(sed -n 's/^CONFIG_APPLY=//p' "$ETC_DIR/uploader.conf" 2>/dev/null | tail -1)
  log "uploader CONFIG_APPLY=${accepting:-0} - $([[ ${accepting:-0} == 1 ]] && echo "configurations are applied" || echo "nothing is applied (delivery off)")"
}

main "$@"
