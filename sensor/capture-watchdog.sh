#!/usr/bin/env bash
#
# capture-watchdog.sh - make sure Kismet is actually receiving frames.
#
# Run every few minutes by wifi-sensor-capture-watchdog.timer (as root,
# installed by install_sensor.sh). Kismet reports a datasource as "running"
# even when the radio delivers nothing (seen after a cold boot: helper crashed
# during bring-up, monitor VIF adopted half-configured, 0 packets for hours),
# so the check is Kismet's own packet counter, read from its REST API.
#
# Escalation, one step per consecutive silent check (with a 2-minute timer,
# STRIKES_RESTART=2 means "no frames for ~4 minutes"):
#   STRIKES_RESTART  -> systemctl restart kismet         (fresh helper + monitor VIF)
#   STRIKES_DRIVER   -> reload the adapter's kernel module, then restart Kismet
#   STRIKES_USB      -> software re-plug of the USB device (authorized 0/1),
#                       restart Kismet, and start the cycle again
# Only the configured capture adapter is ever touched.
#
# Environment (set by the unit):
#   CAPTURE_IFACE      capture adapter, e.g. wlan1                       (required)
#   KISMET_URL         REST base URL           (default http://127.0.0.1:2501)
#   KISMET_AUTH_FILE   kismet_httpd.conf with httpd_username/httpd_password
#   WATCHDOG_STATE_DIR where the last counter and strike count live (default /run/wifi-sensor)
#   STRIKES_RESTART / STRIKES_DRIVER / STRIKES_USB   (defaults 2 / 4 / 6)
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

IFACE=${CAPTURE_IFACE:-}
URL=${KISMET_URL:-http://127.0.0.1:2501}
AUTH_FILE=${KISMET_AUTH_FILE:-}
STATE_DIR=${WATCHDOG_STATE_DIR:-/run/wifi-sensor}
S_RESTART=${STRIKES_RESTART:-2}
S_DRIVER=${STRIKES_DRIVER:-4}
S_USB=${STRIKES_USB:-6}

log()  { echo "capture-watchdog: $*"; }
warn() { echo "capture-watchdog: WARNING: $*" >&2; }

[[ -n $IFACE ]] || { warn "CAPTURE_IFACE is not set"; exit 0; }
mkdir -p "$STATE_DIR"
LAST_FILE=$STATE_DIR/capture-packets
STRIKE_FILE=$STATE_DIR/capture-strikes
DEV_FILE=$STATE_DIR/capture-usb-device
DRV_FILE=$STATE_DIR/capture-driver

read_state() { [[ -s $1 ]] && cat "$1" || echo "$2"; }
reset_state() { echo 0 >"$STRIKE_FILE"; echo "${1:-0}" >"$LAST_FILE"; }

# Remember where the adapter lives while it is visible, so the USB re-plug can
# still find it after the interface has vanished.
if [[ -e /sys/class/net/$IFACE/device ]]; then
  devpath=$(readlink -f "/sys/class/net/$IFACE/device")   # .../1-1.1.2:1.0
  usbdev=$(dirname "$devpath")                            # .../1-1.1.2
  [[ -e $usbdev/authorized ]] && echo "$usbdev" >"$DEV_FILE"
  drv=$(basename "$(readlink -f "$devpath/driver" 2>/dev/null)" 2>/dev/null)
  [[ -n $drv ]] && echo "$drv" >"$DRV_FILE"
fi

# Kismet not running: systemd owns that problem (Restart=always). Start fresh.
if ! systemctl is-active --quiet kismet; then
  reset_state 0
  exit 0
fi

# Sum of num_packets over all datasources, or "" when the API does not answer.
packets=""
if [[ -s $AUTH_FILE ]]; then
  auth=$(awk -F= '$1 == "httpd_username" {u = $2} $1 == "httpd_password" {sub(/^[^=]*=/, ""); p = $0} END {print u ":" p}' "$AUTH_FILE")
  json=$(curl -fsS -m 10 -K - "$URL/datasource/all_sources.json" <<<"user = \"$auth\"" 2>/dev/null || true)
  if [[ -n $json ]]; then
    packets=$(printf '%s' "$json" | python3 -c '
import json, sys
try:
    print(sum(int(s.get("kismet.datasource.num_packets") or 0) for s in json.load(sys.stdin)))
except Exception:
    pass' 2>/dev/null || true)
  fi
else
  warn "auth file $AUTH_FILE missing - cannot query Kismet"
  exit 0
fi

last=$(read_state "$LAST_FILE" 0)
strikes=$(read_state "$STRIKE_FILE" 0)

# Alive = the counter moved. A counter that dropped means Kismet restarted and
# is receiving again; an identical non-zero counter means the source is stuck.
if [[ -n $packets && $packets -gt 0 && $packets -ne $last ]]; then
  if (( strikes > 0 )); then log "capture recovered ($packets packets)"; fi
  reset_state "$packets"
  exit 0
fi

(( strikes += 1 ))
echo "$strikes" >"$STRIKE_FILE"
[[ -n $packets ]] && echo "$packets" >"$LAST_FILE"
reason=${packets:+"packet counter stuck at $packets"}
reason=${reason:-"REST API not answering"}
log "no frames from $IFACE: $reason (strike $strikes)"

if (( strikes == S_RESTART )); then
  log "restarting kismet"
  systemctl restart kismet
elif (( strikes == S_DRIVER )); then
  drv=$(read_state "$DRV_FILE" "")
  if [[ -n $drv ]]; then
    log "reloading driver $drv and restarting kismet"
    systemctl stop kismet
    modprobe -r "$drv" 2>&1 | sed 's/^/capture-watchdog: /' || true
    sleep 2
    modprobe "$drv" 2>&1 | sed 's/^/capture-watchdog: /' || true
    sleep 5
    systemctl start kismet
  else
    warn "driver of $IFACE unknown - skipping module reload"
    systemctl restart kismet
  fi
elif (( strikes >= S_USB )); then
  usbdev=$(read_state "$DEV_FILE" "")
  if [[ -n $usbdev && -e $usbdev/authorized ]]; then
    log "re-plugging USB device $(basename "$usbdev") and restarting kismet"
    systemctl stop kismet
    echo 0 >"$usbdev/authorized"; sleep 3
    echo 1 >"$usbdev/authorized"; sleep 10
    systemctl start kismet
  else
    warn "USB device of $IFACE unknown - restarting kismet only"
    systemctl restart kismet
  fi
  # Start the escalation over rather than hammering the bus every 2 minutes.
  echo 0 >"$STRIKE_FILE"
fi
exit 0
